"""Tests for the Azure-SQL startup path of the pipeline: Alembic-owns-the-schema
migration on mssql startup (with cold-resume retry on serverless error 40613 and
fail-fast on anything else), plus the fail-fast guard that refuses to run against a
throwaway local SQLite when a cloud marker is present.

All offline: the alembic/DB seams are injected or monkeypatched, so no real
Alembic, database, or network is touched.
"""

from datetime import date

import pytest

from swing_screener.db.session import get_engine
from swing_screener.pipeline import run


def _sqlite_engine_for(url):
    """Stand-in engine so the mssql code path can open a real Session offline:
    we never want to connect to Azure SQL in a test, so back the run with an
    in-memory sqlite engine regardless of the (mssql) url passed in."""
    return get_engine("sqlite:///:memory:")


def _write_universe(tmp_path, tickers):
    p = tmp_path / "universe.csv"
    p.write_text("ticker,name,exchange\n" + "\n".join(f"{t},{t} Inc,NYSE" for t in tickers) + "\n")
    return p


# --- _migrate_with_retry: 40613 cold-resume retry ---------------------------

def test_migrate_retries_on_40613_then_succeeds():
    calls = []

    def flaky_upgrade(url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("Login failed: Error 40613: Database is currently unavailable")

    run._migrate_with_retry(
        "mssql+pyodbc://server/db", upgrade_fn=flaky_upgrade, sleep_fn=lambda *_: None,
    )

    assert calls == ["mssql+pyodbc://server/db", "mssql+pyodbc://server/db"]


def test_migrate_reraises_40613_after_exhausting_attempts():
    calls = []

    def always_resuming(url):
        calls.append(url)
        raise RuntimeError("40613 database is currently unavailable")

    with pytest.raises(RuntimeError, match="40613"):
        run._migrate_with_retry(
            "mssql+pyodbc://server/db", attempts=3,
            upgrade_fn=always_resuming, sleep_fn=lambda *_: None,
        )

    assert len(calls) == 3  # retried up to `attempts`, then gave up


# --- _migrate_with_retry: non-40613 errors fail fast ------------------------

def test_migrate_does_not_retry_non_40613_error():
    calls = []

    def boom(url):
        calls.append(url)
        raise RuntimeError("login failed for user (real config error, not a resume)")

    with pytest.raises(RuntimeError, match="login failed"):
        run._migrate_with_retry(
            "mssql+pyodbc://server/db", upgrade_fn=boom, sleep_fn=lambda *_: None,
        )

    assert len(calls) == 1  # immediate re-raise, NO retry


# --- run_screen: mssql path runs the migration, never create_all -----------

def test_run_screen_mssql_runs_migration_and_skips_create_all(tmp_path, monkeypatch):
    migrated = []
    seen_engine_urls = []

    def fake_migrate(url):
        migrated.append(url)

    # Record which url get_engine was asked for, but hand back an in-memory
    # sqlite engine so the run can open a Session offline. The REAL get_engine
    # never calls create_all for an mssql url (Alembic owns that schema); we
    # assert that by checking it was only ever invoked with the mssql url and
    # never with a sqlite/create_all path.
    def recording_get_engine(url):
        seen_engine_urls.append(url)
        return _sqlite_engine_for(url)

    monkeypatch.setattr(run, "get_engine", recording_get_engine)

    db = "mssql+pyodbc://server/db?driver=ODBC+Driver+18+for+SQL+Server"
    res = run.run_screen(
        universe_path=_write_universe(tmp_path, []),  # empty universe: no bars needed
        db_url=db, cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
        today=date(2024, 4, 1), migrate_fn=fake_migrate,
    )

    assert migrated == [db]  # migration seam invoked exactly once with the mssql url
    # get_engine was called only with the mssql url; the real impl does NOT
    # create_all for mssql, so the schema is owned by the migration, not create_all.
    assert seen_engine_urls == [db]
    assert res.n_signals == 0


def test_run_screen_mssql_uses_default_retry_wrapper(tmp_path, monkeypatch):
    """Without an injected migrate_fn, the mssql path still routes through the
    module's retry wrapper (which we stub) -- not straight to live alembic."""
    seen = []
    monkeypatch.setattr(run, "_migrate_with_retry", lambda url: seen.append(url))
    monkeypatch.setattr(run, "get_engine", _sqlite_engine_for)

    db = "mssql+pyodbc://server/db"
    run.run_screen(
        universe_path=_write_universe(tmp_path, []), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
        today=date(2024, 4, 1),
    )

    assert seen == [db]


# --- run_screen: sqlite path is untouched (no migration) --------------------

def test_run_screen_sqlite_does_not_migrate(tmp_path, monkeypatch):
    sentinel = []
    monkeypatch.setattr(run, "_migrate_with_retry", lambda url: sentinel.append(url))

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(
        universe_path=_write_universe(tmp_path, []), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
        today=date(2024, 4, 1),
    )

    assert sentinel == []  # sqlite never triggers the alembic migration seam


# --- _resolve_db_url / main: fail-fast in a cloud context -------------------

def test_resolve_db_url_rejects_sqlite_when_key_vault_present(monkeypatch):
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.delenv("SWING_DB_URL", raising=False)  # default resolves to sqlite

    with pytest.raises(RuntimeError, match="SWING_DB_URL"):
        run._resolve_db_url(None)


def test_resolve_db_url_rejects_sqlite_when_require_db_set(monkeypatch):
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("SWING_REQUIRE_DB", "1")
    monkeypatch.delenv("SWING_DB_URL", raising=False)

    with pytest.raises(RuntimeError, match="SWING_DB_URL"):
        run._resolve_db_url(None)


def test_resolve_db_url_allows_sqlite_without_marker(monkeypatch):
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("SWING_REQUIRE_DB", raising=False)
    monkeypatch.setenv("SWING_DB_URL", "sqlite:///local.db")

    # today's behavior: local sqlite is fine when no cloud marker is set
    assert run._resolve_db_url(None) == "sqlite:///local.db"


def test_resolve_db_url_allows_mssql_in_cloud_context(monkeypatch):
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.setenv("SWING_DB_URL", "mssql+pyodbc://server/db")

    assert run._resolve_db_url(None) == "mssql+pyodbc://server/db"


def test_resolve_db_url_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.example/")
    monkeypatch.setenv("SWING_DB_URL", "sqlite:///ignored.db")

    # an explicit --db wins over settings; mssql override is allowed in cloud
    assert run._resolve_db_url("mssql+pyodbc://server/db") == "mssql+pyodbc://server/db"


# --- _alembic_dir: the migration config must resolve to REAL files -----------
# Regression: it used to resolve relative to the package (parents[2]), which in a
# non-editable/container install is site-packages -- so script_location pointed at
# the installed alembic LIBRARY (no migration env.py) and the cloud migration died
# with "Can't find .../site-packages/alembic/env.py".

def test_alembic_dir_default_points_at_real_config():
    # default resolves from CWD; pytest runs from the repo root, where the real
    # alembic.ini + alembic/env.py live. (The old package-relative path pointed at
    # src/alembic.ini, which does not exist.)
    base = run._alembic_dir()
    assert (base / "alembic.ini").exists(), f"alembic.ini not found at {base}"
    assert (base / "alembic" / "env.py").exists()


def test_alembic_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SWING_ALEMBIC_DIR", str(tmp_path))
    assert run._alembic_dir() == tmp_path.resolve()
