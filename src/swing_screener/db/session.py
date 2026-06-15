"""Engine/session helpers for the screener's SQLite (later Azure SQL) store."""

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.pool import StaticPool

from swing_screener.db.models import Base


def _mssql_odbc_url(url: str) -> URL:
    """Rebuild an mssql URL into the explicit ``odbc_connect`` form.

    A username-less Azure SQL URL (managed-identity auth) makes SQLAlchemy's
    pyodbc dialect auto-add ``Trusted_Connection=Yes``, which conflicts with
    ``Authentication=ActiveDirectoryMSI`` -- ODBC fails with FA001 ("Cannot use
    Authentication option with Integrated Security option"). Handing pyodbc the
    exact ODBC connection string via ``odbc_connect`` stops SQLAlchemy injecting
    anything. A URL that already uses ``odbc_connect`` is passed through.
    """
    u = make_url(url)
    if "odbc_connect" in u.query:
        return u

    def first(v: str | tuple[str, ...]) -> str:
        return v if isinstance(v, str) else v[0]

    driver = first(u.query.get("driver", "ODBC Driver 18 for SQL Server"))
    server = u.host or ""
    if u.port:
        server = f"{server},{u.port}"
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}"]
    if u.database:
        parts.append(f"DATABASE={u.database}")
    for key, val in u.query.items():
        if key.lower() != "driver":  # already placed above
            parts.append(f"{key}={first(val)}")
    return URL.create("mssql+pyodbc", query={"odbc_connect": ";".join(parts)})


def to_connect_url(url: str) -> str | URL:
    """The URL to hand ``create_engine``: the ``odbc_connect`` form for mssql
    (avoids the FA001 Trusted_Connection clash), the string unchanged otherwise.

    Shared by ``get_engine`` AND Alembic's ``env.py`` so the startup migration
    and the screen's writes connect to Azure SQL the same way.
    """
    return _mssql_odbc_url(url) if url.startswith("mssql") else url


def get_engine(url: str = "sqlite:///swing_screener.db") -> Engine:
    """Create an engine and ensure all tables exist.

    In-memory SQLite uses a ``StaticPool`` so the same in-memory database is
    shared across sessions/connections instead of being recreated per-connect.
    """
    kwargs: dict[str, object] = {}
    if url.startswith("mssql"):
        # Azure SQL: Alembic owns the schema, so we never call create_all (it
        # cannot ALTER existing tables anyway). pool_pre_ping validates
        # connections and pool_recycle drops stale ones so the engine survives
        # serverless auto-pause/resume. to_connect_url avoids the
        # Trusted_Connection/Authentication conflict (see _mssql_odbc_url).
        return create_engine(to_connect_url(url), pool_pre_ping=True, pool_recycle=3600)
    if url.endswith(":memory:"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine
