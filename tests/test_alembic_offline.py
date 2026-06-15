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

pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {"universe", "signals", "trades", "paper_trades", "exit_events", "email_log"}
TICKER_INDEXES = {"ix_signals_ticker", "ix_trades_ticker", "ix_paper_trades_ticker"}


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
