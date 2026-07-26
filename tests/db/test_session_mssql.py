"""Tests for ``get_engine`` branching across sqlite (memory/file) and mssql.

No real DB driver, azure token, or network is exercised: ``create_engine``,
``Base.metadata.create_all`` and the AAD-token listener are monkeypatched.
"""

from sqlalchemy.pool import StaticPool

from swing_screener.db import models, session


class _CreateEngineRecorder:
    def __init__(self):
        self.url = None
        self.kwargs: dict[str, object] = {}
        self.calls = 0

    def __call__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.calls += 1
        return object()  # sentinel engine; never connected to


class _CallRecorder:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1


def _patch(monkeypatch):
    rec_engine = _CreateEngineRecorder()
    rec_create_all = _CallRecorder()
    rec_token = _CallRecorder()
    monkeypatch.setattr(session, "create_engine", rec_engine)
    monkeypatch.setattr(models.Base.metadata, "create_all", rec_create_all)
    # don't attach a real do_connect listener to the sentinel engine, and never
    # import azure / acquire a token in tests.
    monkeypatch.setattr(session, "_attach_aad_token", rec_token)
    return rec_engine, rec_create_all, rec_token


def test_mssql_token_auth_clean_odbc_and_skips_create_all(monkeypatch):
    rec_engine, rec_create_all, rec_token = _patch(monkeypatch)
    url = ("mssql+pyodbc://@host.database.windows.net:1433/swing"
           "?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMSI&User+Id=CID")

    session.get_engine(url)

    assert rec_engine.kwargs.get("pool_pre_ping") is True
    assert rec_engine.kwargs.get("pool_recycle") == 3600
    assert rec_create_all.calls == 0  # Alembic owns the Azure SQL schema
    assert rec_token.calls == 1  # a managed-identity access token is injected on connect
    # the ODBC string carries NO auth keywords -- the token authenticates, so
    # neither Trusted_Connection (FA001) nor Authentication/MSI (HYT00) appear.
    odbc = rec_engine.url.query["odbc_connect"]
    for banned in ("Trusted_Connection", "Authentication", "User Id", "UID="):
        assert banned not in odbc, f"{banned} should be stripped: {odbc}"
    assert "DATABASE=swing" in odbc and "Encrypt=yes" in odbc


def test_mssql_passes_through_existing_odbc_connect(monkeypatch):
    rec_engine, _, _ = _patch(monkeypatch)
    url = "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BX%7D%3BSERVER%3Dh%3BDATABASE%3Dswing"

    session.get_engine(url)

    assert "DATABASE=swing" in rec_engine.url.query["odbc_connect"]


def test_memory_sqlite_uses_static_pool_and_creates_tables(monkeypatch):
    rec_engine, rec_create_all, _ = _patch(monkeypatch)

    session.get_engine("sqlite:///:memory:")

    assert rec_engine.kwargs.get("poolclass") is StaticPool
    assert rec_engine.kwargs.get("connect_args") == {"check_same_thread": False}
    assert rec_create_all.calls == 1


def test_file_sqlite_creates_tables_without_static_pool(monkeypatch):
    rec_engine, rec_create_all, _ = _patch(monkeypatch)

    session.get_engine("sqlite:///somefile.db")

    assert "poolclass" not in rec_engine.kwargs
    assert rec_create_all.calls == 1


import struct
import sys
import types

from sqlalchemy import create_engine


def test_token_attrs_packs_utf16_length_prefixed():
    """The pyodbc access-token attribute: UTF-16-LE token bytes, little-endian
    length-prefixed, under SQL_COPT_SS_ACCESS_TOKEN (1256)."""
    raw = "abc".encode("utf-16-le")
    assert session._token_attrs("abc") == {1256: struct.pack("<i", len(raw)) + raw}


def test_static_env_token_skips_azure_identity(monkeypatch):
    """With SWING_DB_ACCESS_TOKEN set (CI pre-fetches the DB token while the OIDC
    assertion is still alive), the connect listener must use it directly -- no
    azure.identity import, no credential chain. Poisoning the module proves it."""
    monkeypatch.setenv("SWING_DB_ACCESS_TOKEN", "tok")
    monkeypatch.setitem(sys.modules, "azure.identity", None)  # import would raise
    engine = create_engine("sqlite://")  # never connected; just a listener target

    session._attach_aad_token(engine)  # must not touch azure.identity

    # do_connect is a DIALECT event: the listener migrates to the dialect dispatch.
    assert engine.dialect.dispatch.do_connect  # the token listener is registered


def test_without_env_token_uses_default_azure_credential(monkeypatch):
    """The normal path (no pre-fetched token) still builds DefaultAzureCredential."""
    monkeypatch.delenv("SWING_DB_ACCESS_TOKEN", raising=False)
    calls = {"n": 0}

    class _FakeCredential:
        def __init__(self):
            calls["n"] += 1

    monkeypatch.setitem(sys.modules, "azure.identity",
                        types.SimpleNamespace(DefaultAzureCredential=_FakeCredential))
    engine = create_engine("sqlite://")

    session._attach_aad_token(engine)

    assert calls["n"] == 1
    assert engine.dialect.dispatch.do_connect


from swing_screener.db.session import _best_sql_server_driver, _resolve_driver


def test_best_driver_picks_highest_numbered():
    avail = ["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]
    assert _best_sql_server_driver(avail) == "ODBC Driver 18 for SQL Server"


def test_best_driver_none_when_absent():
    assert _best_sql_server_driver(["Microsoft Access Driver (*.mdb)"]) is None


def test_resolve_driver_keeps_requested_when_installed():
    avail = ["ODBC Driver 18 for SQL Server"]
    assert _resolve_driver("ODBC Driver 18 for SQL Server", avail) == "ODBC Driver 18 for SQL Server"


def test_resolve_driver_falls_back_to_installed_when_requested_missing():
    avail = ["ODBC Driver 17 for SQL Server"]
    assert _resolve_driver("ODBC Driver 18 for SQL Server", avail) == "ODBC Driver 17 for SQL Server"


def test_resolve_driver_keeps_requested_when_nothing_installed():
    assert _resolve_driver("ODBC Driver 18 for SQL Server", []) == "ODBC Driver 18 for SQL Server"
