"""Tests for ``get_engine`` branching across sqlite (memory/file) and mssql.

No real DB driver or network is exercised: ``create_engine`` and
``Base.metadata.create_all`` are monkeypatched with recorders.
"""

from sqlalchemy.pool import StaticPool

from swing_screener.db import models, session


class _CreateEngineRecorder:
    def __init__(self):
        self.url: str | None = None
        self.kwargs: dict[str, object] = {}
        self.calls = 0

    def __call__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.calls += 1
        return object()  # sentinel engine; never connected to


class _CreateAllRecorder:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1


def _patch(monkeypatch):
    rec_engine = _CreateEngineRecorder()
    rec_create_all = _CreateAllRecorder()
    monkeypatch.setattr(session, "create_engine", rec_engine)
    monkeypatch.setattr(models.Base.metadata, "create_all", rec_create_all)
    return rec_engine, rec_create_all


def test_mssql_uses_health_pooling_and_skips_create_all(monkeypatch):
    rec_engine, rec_create_all = _patch(monkeypatch)
    url = "mssql+pyodbc://user@host/db?driver=ODBC+Driver+18+for+SQL+Server"

    session.get_engine(url)

    assert rec_engine.url == url
    assert rec_engine.kwargs.get("pool_pre_ping") is True
    assert rec_engine.kwargs.get("pool_recycle") == 3600
    # Alembic owns the Azure SQL schema; create_all must not run.
    assert rec_create_all.calls == 0


def test_memory_sqlite_uses_static_pool_and_creates_tables(monkeypatch):
    rec_engine, rec_create_all = _patch(monkeypatch)

    session.get_engine("sqlite:///:memory:")

    assert rec_engine.kwargs.get("poolclass") is StaticPool
    assert rec_engine.kwargs.get("connect_args") == {"check_same_thread": False}
    assert rec_create_all.calls == 1


def test_file_sqlite_creates_tables_without_static_pool(monkeypatch):
    rec_engine, rec_create_all = _patch(monkeypatch)

    session.get_engine("sqlite:///somefile.db")

    assert "poolclass" not in rec_engine.kwargs
    assert rec_create_all.calls == 1
