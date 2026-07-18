"""Offline checks that the Alembic initial migration is complete and correct.

These run only when ``alembic`` is importable (it ships in the optional ``azure``
extra, so CI -- which installs only ``.[dev]`` -- skips them). The migration is
driven against a throwaway SQLite database (no driver/network), proving it
creates every table and the indexes/constraints the app relies on. The
mssql-specific rendering (VARCHAR(n) not (max), BIT, IDENTITY) is additionally
guarded driverlessly in ``test_models_schema.py`` and was verified against a
live SQL Server during authoring.
"""

import sqlite3
from pathlib import Path

import pytest

# Import the real submodule, not just "alembic": the repo's top-level alembic/
# migrations dir is importable as a namespace package, so importorskip("alembic")
# would falsely succeed when the PyPI package (azure extra) is absent. alembic
# ships in the optional azure extra, so CI (.[dev] only) skips these.
pytest.importorskip("alembic.command")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"universe", "signals", "trades", "paper_trades", "exit_events",
                   "email_log", "analyst_calls", "execution_logs", "reversal_funnels"}
TICKER_INDEXES = {"ix_signals_ticker", "ix_trades_ticker", "ix_paper_trades_ticker",
                  "ix_analyst_calls_ticker", "ix_execution_logs_ticker"}
# slicing indexes the shadow book relies on (arm/variant/account A/B + isolation).
PAPER_TRADE_INDEXES = {"ix_paper_trades_arm", "ix_paper_trades_variant",
                       "ix_paper_trades_account"}
# the exit log is sliced by account (research grid vs the curated intent book).
EXIT_EVENT_INDEXES = {"ix_exit_events_account"}
# Phase 4 live-broker tracking: execution_logs is looked up by broker + broker order id.
EXECUTION_LOG_BROKER_INDEXES = {"ix_execution_logs_broker",
                                "ix_execution_logs_broker_order_id"}
# one funnel snapshot per daily digest: the unique index is the idempotency backstop
# behind the write site's delete-then-insert.
REVERSAL_FUNNEL_INDEXES = {"uq_reversal_funnels_run_date"}


def _config(db_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_creates_every_table_and_index(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)  # env.py reads the URL from settings
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()

    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables  # migration actually stamped a revision
    assert TICKER_INDEXES <= indexes
    assert PAPER_TRADE_INDEXES <= indexes
    assert EXIT_EVENT_INDEXES <= indexes
    assert EXECUTION_LOG_BROKER_INDEXES <= indexes
    assert REVERSAL_FUNNEL_INDEXES <= indexes


def test_migration_adds_analyst_call_token_spend_columns(tmp_path, monkeypatch):
    # The Phase-6 cost-capture migration adds four nullable token-spend columns to
    # analyst_calls; assert the upgraded schema actually carries them.
    db = tmp_path / "t.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(analyst_calls)")}
    finally:
        con.close()

    assert {"input_tokens", "output_tokens", "web_searches", "est_cost_usd"} <= cols


def test_migration_adds_analysis_request_est_cost_column(tmp_path, monkeypatch):
    # E3c: the uncapped on-demand path gets cost visibility -- analysis_requests
    # carries an APPROXIMATE est_cost_usd. Nullable, NO server_default (NULL means
    # a fallback/legacy row where no billed call was captured, never a fake $0).
    db = tmp_path / "ac.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(analysis_requests)")}
    finally:
        con.close()

    assert "est_cost_usd" in info
    assert info["est_cost_usd"][3] == 0     # nullable (notnull flag off)
    assert info["est_cost_usd"][4] is None  # no server default


def test_migration_adds_market_report_est_cost_column(tmp_path, monkeypatch):
    # E6: the weekly Market Weather LLM call gets spend visibility -- market_reports
    # carries an APPROXIMATE est_cost_usd. Nullable, NO server_default (NULL means a
    # deterministic/fallback/legacy row with no billed call captured, never a fake $0).
    db = tmp_path / "mw.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(market_reports)")}
    finally:
        con.close()

    assert "est_cost_usd" in info
    assert info["est_cost_usd"][3] == 0     # nullable (notnull flag off)
    assert info["est_cost_usd"][4] is None  # no server default


def test_migration_adds_trade_override_column(tmp_path, monkeypatch):
    # The cockpit's log-trade action stamps HOW a fill deviated from the engine's
    # plan into trades.override -- nullable, NO server_default (NULL means
    # engine-faithful or unprefilled). Assert the upgraded schema carries it.
    db = tmp_path / "ov.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(trades)")}
    finally:
        con.close()

    assert "override" in info
    assert info["override"][3] == 0     # nullable (notnull flag off)
    assert info["override"][4] is None  # no server default


def test_migration_enforces_reversal_funnel_run_date_unique(tmp_path, monkeypatch):
    # ONE funnel row per run_date: the unique index must reject a duplicate (the
    # backstop behind the write site's delete-then-insert on a forced resend).
    db = tmp_path / "rf.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        # every count column carries a server_default, so run_date alone suffices
        con.execute("INSERT INTO reversal_funnels (run_date) VALUES ('2026-07-09')")
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO reversal_funnels (run_date) VALUES ('2026-07-09')")
            con.commit()
    finally:
        con.close()


def test_migration_creates_journal_v2_tables(tmp_path, monkeypatch):
    # Journal v2 adds the two coaches' artifact tables + the profile + a disarm log.
    db = tmp_path / "jv2.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()

    assert {"journal_reviews", "system_audits", "weaknesses_profiles",
            "disarm_events", "coach_draft_requests"} <= tables


def test_migration_adds_trade_emotional_state_column(tmp_path, monkeypatch):
    # emotional_state is nullable, no server_default (manual actions only; NULL = none).
    db = tmp_path / "emo.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(trades)")}
    finally:
        con.close()

    assert "emotional_state" in info
    assert info["emotional_state"][3] == 0     # nullable
    assert info["emotional_state"][4] is None  # no server default


def test_migration_enforces_journal_review_identity_unique(tmp_path, monkeypatch):
    # A re-fired on-close/rollup job must not double-write: identity_key is UNIQUE.
    db = tmp_path / "jr.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        row = ("INSERT INTO journal_reviews (identity_key, kind, book, source) "
               "VALUES ('trade_close:manual_equity:1', 'trade_close', 'manual_equity', 'analyst')")
        con.execute(row)
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(row)
            con.commit()
    finally:
        con.close()


def test_migration_enforces_system_audit_identity_unique(tmp_path, monkeypatch):
    # One weekly audit per period: the composite unique (all NOT NULL) rejects a dup.
    db = tmp_path / "sa.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        row = ("INSERT INTO system_audits (kind, period_from, period_to, breach_key) "
               "VALUES ('weekly', '2026-07-06', '2026-07-12', '')")
        con.execute(row)
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(row)
            con.commit()
    finally:
        con.close()


def test_migration_creates_hot_path_indexes(tmp_path, monkeypatch):
    # Perf: the three hot-path indexes (2026-07-17 audit, I1). signals.run_date
    # backs latest_run_date / latest_signals / delete_signals_for / prior_first_seen
    # (the cockpit picks poll full-scanned a forever-growing table without it);
    # (status, account) backs the open/pending/closed loaders plus the pre-trade
    # cap gate and per-day-loss breaker; exit_events.created_date backs the hourly
    # exit-checker dedup and the reference-screen sort.
    db = tmp_path / "hp.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        composite_cols = [
            r[2] for r in con.execute("PRAGMA index_info('ix_paper_trades_status_account')")
        ]
    finally:
        con.close()

    assert {"ix_signals_run_date", "ix_paper_trades_status_account",
            "ix_exit_events_created_date"} <= indexes
    # column order matters: every gate query filters status by equality first,
    # account by equality/inequality second (or not at all), so status leads.
    assert composite_cols == ["status", "account"]


def test_migration_makes_import_key_unique_index_filtered(tmp_path, monkeypatch):
    # option_paper_trades.import_key is NULLABLE (paper trades skip it) but its
    # unique index was created PLAIN (f2a9c4e7b1d8). On SQL Server a plain unique
    # index admits only ONE NULL row -- the second key-less paper trade would be
    # rejected. The fix recreates it as a FILTERED unique index (WHERE import_key
    # IS NOT NULL).
    #
    # sqlite-vs-mssql proof boundary: sqlite's plain UNIQUE index already allows
    # multiple NULLs, so the two-NULLs insert below cannot distinguish the broken
    # mssql schema from the fixed one on sqlite. What sqlite CAN prove is (a) the
    # index carries the partial WHERE clause (PRAGMA index_list partial flag --
    # this is the assertion that fails against the pre-fix plain index) and
    # (b) duplicate non-NULL keys are still rejected (import idempotency intact).
    # The single-NULL mssql semantics itself is only exercised on a real SQL
    # Server; the mssql_where rendering is pinned by the migration + model.
    db = tmp_path / "ik.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        idx = {
            r[1]: r for r in con.execute("PRAGMA index_list('option_paper_trades')")
        }
        assert "uq_option_paper_trades_import_key" in idx
        assert idx["uq_option_paper_trades_import_key"][2] == 1  # unique
        assert idx["uq_option_paper_trades_import_key"][4] == 1  # partial (filtered)

        # two key-less rows coexist (the mssql failure mode this fix targets)...
        con.execute("INSERT INTO option_paper_trades (underlying) VALUES ('SPY')")
        con.execute("INSERT INTO option_paper_trades (underlying) VALUES ('QQQ')")
        con.commit()
        # ...while a duplicate non-NULL key is still rejected (import idempotency).
        con.execute(
            "INSERT INTO option_paper_trades (underlying, import_key) VALUES ('SPY', 'k1')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO option_paper_trades (underlying, import_key) VALUES ('SPY', 'k1')"
            )
            con.commit()
    finally:
        con.close()


def test_migration_adds_option_setup_play_type_and_autograde_json(tmp_path, monkeypatch):
    # T2: option_setups gains play_type (NOT NULL, server_default "" -- legacy/
    # unspecified rows backfill to empty) and autograde_json (nullable Text -- the
    # machine per-item provenance; NULL means no auto-grade ran, never a fake grade).
    db = tmp_path / "os.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        info = {r[1]: r for r in con.execute("PRAGMA table_info(option_setups)")}
    finally:
        con.close()

    assert "play_type" in info
    assert info["play_type"][3] == 1          # NOT NULL
    assert info["play_type"][4] == "''"       # server_default '' backfills existing rows
    assert "autograde_json" in info
    assert info["autograde_json"][3] == 0     # nullable (notnull flag off)
    assert info["autograde_json"][4] is None  # no server default -- NULL = no grade ran


def test_migration_option_setup_columns_reversible(tmp_path, monkeypatch):
    # Up/down/up cycle on scratch sqlite: the downgrade drops both columns and a
    # re-upgrade restores them, so the migration is a clean round-trip.
    db = tmp_path / "osr.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    # Downgrade to the revision BELOW the option_setup migration by id, not "-1":
    # relative steps break the moment a later migration tops the chain (the lab
    # migration c1d7f3e9a5b2 did exactly that) -- the target is this migration's
    # own down_revision, so the test keeps reversing THIS migration forever.
    command.downgrade(cfg, "b4e9f2c7a3d1")

    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(option_setups)")}
    finally:
        con.close()
    assert "play_type" not in cols and "autograde_json" not in cols

    command.upgrade(cfg, "head")  # up again: the cycle is clean
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(option_setups)")}
    finally:
        con.close()
    assert {"play_type", "autograde_json"} <= cols


def test_migration_enforces_email_log_dedup(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("SWING_DB_URL", url)
    command.upgrade(_config(url), "head")

    con = sqlite3.connect(db)
    try:
        row = "(?, 'daily', 's', '2026-06-15', '')"
        con.execute(f"INSERT INTO email_log (sent_at, kind, subject, run_date, alert_key) VALUES {row}",
                    ("2026-06-15",))
        con.commit()
        # the uq_email_log_dedup unique constraint must reject a duplicate triple
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(f"INSERT INTO email_log (sent_at, kind, subject, run_date, alert_key) VALUES {row}",
                        ("2026-06-15",))
            con.commit()
    finally:
        con.close()
