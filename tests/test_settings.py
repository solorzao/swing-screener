"""Tests for the env-first settings module.

Settings must read ``os.environ`` at CALL time (not import time) so tests can
monkeypatch env, call ``load_settings()``, and see the change. Path defaults
must resolve to ABSOLUTE paths (relative defaults are wrong in a container).
"""

from pathlib import Path

from swing_screener.settings import Settings, load_settings

# the repo-relative defaults the dashboard/orchestrator used before this refactor
_OLD_CHART = Path(".charts").resolve()
_OLD_CACHE = Path(".cache").resolve()
_OLD_PDF = Path(".digests").resolve()


def _clear_env(monkeypatch):
    for name in (
        "SWING_DB_URL", "SWING_CHART_DIR", "SWING_CACHE_DIR", "SWING_PDF_DIR",
        "SWING_BLOB_ACCOUNT_URL", "SWING_BLOB_CONTAINER", "KEY_VAULT_URL",
        "AZURE_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_old_repo_relative_resolved(monkeypatch):
    _clear_env(monkeypatch)
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.db_url == "sqlite:///local.db"
    assert s.blob_container == "charts"
    assert s.blob_account_url is None
    assert s.key_vault_url is None
    assert s.azure_client_id is None
    # path defaults are ABSOLUTE and equal the old repo-relative defaults resolved
    assert s.chart_dir == _OLD_CHART and s.chart_dir.is_absolute()
    assert s.cache_dir == _OLD_CACHE and s.cache_dir.is_absolute()
    assert s.pdf_dir == _OLD_PDF and s.pdf_dir.is_absolute()


def test_explicit_abs_chart_dir_used_exactly(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    abs_dir = tmp_path / "data" / "charts"
    monkeypatch.setenv("SWING_CHART_DIR", str(abs_dir))
    s = load_settings()
    assert s.chart_dir == abs_dir.resolve()
    assert s.chart_dir.is_absolute()


def test_all_scalar_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SWING_DB_URL", "mssql+pyodbc://host/db")
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    monkeypatch.setenv("SWING_BLOB_CONTAINER", "mycharts")
    monkeypatch.setenv("KEY_VAULT_URL", "https://vault.vault.azure.net")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-123")
    s = load_settings()
    assert s.db_url == "mssql+pyodbc://host/db"
    assert s.blob_account_url == "https://acct.blob.core.windows.net"
    assert s.blob_container == "mycharts"
    assert s.key_vault_url == "https://vault.vault.azure.net"
    assert s.azure_client_id == "client-123"


def test_env_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    first = tmp_path / "first"
    monkeypatch.setenv("SWING_PDF_DIR", str(first))
    assert load_settings().pdf_dir == first.resolve()
    # change env, call again -> the result changes (proves no import-time capture)
    second = tmp_path / "second"
    monkeypatch.setenv("SWING_PDF_DIR", str(second))
    assert load_settings().pdf_dir == second.resolve()


def test_settings_is_frozen(monkeypatch):
    _clear_env(monkeypatch)
    s = load_settings()
    raised = False
    try:
        s.db_url = "x"  # type: ignore[misc]
    except Exception:
        raised = True
    assert raised
