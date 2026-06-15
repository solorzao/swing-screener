"""Engine/session helpers for the screener's SQLite (later Azure SQL) store."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from swing_screener.db.models import Base


def get_engine(url: str = "sqlite:///swing_screener.db") -> Engine:
    """Create an engine and ensure all tables exist.

    In-memory SQLite uses a ``StaticPool`` so the same in-memory database is
    shared across sessions/connections instead of being recreated per-connect.
    """
    kwargs: dict[str, object] = {}
    if url.startswith("mssql"):
        # Azure SQL: Alembic owns the schema, so we never call create_all
        # (it cannot ALTER existing tables anyway). pool_pre_ping validates
        # connections and pool_recycle drops stale ones so the engine survives
        # serverless auto-pause/resume that silently kills idle connections.
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    if url.endswith(":memory:"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine
