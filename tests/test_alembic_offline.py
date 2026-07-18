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
