"""Tests for the digest/exit CLI's Azure-SQL startup path (``notify.run main()``):
the sqlite-in-cloud fail-fast guard and the Alembic self-migration on mssql,
mirroring the other job entrypoints (pipeline.run, coach_run, audit_run, ondemand).

All offline: ``send_digest`` / ``run_exit_check_and_alert`` / the migration seam
are monkeypatched, so no email, model call, or real database is touched.
"""

import pytest

from swing_screener.notify import run as nrun
from swing_screener.pipeline.exitcheck import ExitCheckResult

_MSSQL = "mssql+pyodbc://server/db?driver=ODBC+Driver+18+for+SQL+Server"


def _no_cloud(monkeypatch):
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("SWING_REQUIRE_DB", raising=False)


def _digest_recorder(calls):
    def fake_send_digest(**kw):
        calls.append(("digest", kw["db_url"]))
        return nrun.DigestResult(n_picks=0, pdf_attached=False, sent=False)
    return fake_send_digest


# --- fail-fast: local sqlite in a cloud context ------------------------------

def test_main_refuses_sqlite_default_when_key_vault_present(monkeypatch):
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.delenv("SWING_DB_URL", raising=False)  # default resolves to sqlite
    monkeypatch.setattr(
        nrun, "send_digest",
        lambda **kw: pytest.fail("send_digest must not run against throwaway sqlite"))
    monkeypatch.setattr("sys.argv", ["notify-run", "--kind", "daily"])

    with pytest.raises(RuntimeError, match="SWING_DB_URL"):
        nrun.main()


# --- mssql: migrate FIRST, then run ------------------------------------------

def test_main_migrates_before_digest_on_mssql(monkeypatch):
    _no_cloud(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(nrun, "_migrate_with_retry",
                        lambda url: calls.append(("migrate", url)))
    monkeypatch.setattr(nrun, "send_digest", _digest_recorder(calls))
    monkeypatch.setattr("sys.argv", ["notify-run", "--kind", "daily", "--db", _MSSQL])

    nrun.main()

    assert calls == [("migrate", _MSSQL), ("digest", _MSSQL)]  # migrate BEFORE send


def test_main_migrates_before_exit_check_on_mssql(monkeypatch):
    _no_cloud(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(nrun, "_migrate_with_retry",
                        lambda url: calls.append(("migrate", url)))

    def fake_exit_check(**kw):
        calls.append(("exit", kw["db_url"]))
        return ExitCheckResult(n_open=0, n_exited=0)

    monkeypatch.setattr(nrun, "run_exit_check_and_alert", fake_exit_check)
    monkeypatch.setattr("sys.argv", ["notify-run", "--kind", "exit", "--db", _MSSQL])

    nrun.main()

    assert calls == [("migrate", _MSSQL), ("exit", _MSSQL)]


# --- local sqlite without a cloud marker: no migration, still runs -----------

def test_main_sqlite_local_skips_migration(monkeypatch, tmp_path):
    _no_cloud(monkeypatch)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(nrun, "_migrate_with_retry",
                        lambda url: calls.append(("migrate", url)))
    monkeypatch.setattr(nrun, "send_digest", _digest_recorder(calls))
    db = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setattr("sys.argv", ["notify-run", "--kind", "daily", "--db", db])

    nrun.main()

    assert calls == [("digest", db)]  # ran, and never touched the alembic seam
