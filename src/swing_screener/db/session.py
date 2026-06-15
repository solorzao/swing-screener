"""Engine/session helpers for the screener's SQLite (later Azure SQL) store."""

import struct

from sqlalchemy import URL, Engine, create_engine, event, make_url
from sqlalchemy.pool import StaticPool

from swing_screener.db.models import Base

# pyodbc: SQL_COPT_SS_ACCESS_TOKEN -- pass an Entra access token via attrs_before.
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_DB_TOKEN_SCOPE = "https://database.windows.net/.default"
# ODBC keywords we strip from an mssql URL: auth is via the access token instead.
_AUTH_KEYS = {"driver", "authentication", "uid", "user id", "pwd", "password", "trusted_connection"}


def _mssql_odbc_url(url: str) -> URL:
    """Rebuild an mssql URL into the explicit ``odbc_connect`` form, WITHOUT any
    auth keywords -- a managed-identity access token authenticates instead.

    Two problems are avoided: (1) a username-less URL makes SQLAlchemy auto-add
    ``Trusted_Connection=Yes``, which clashes with ``Authentication`` (ODBC FA001);
    (2) the driver's own ``ActiveDirectoryMSI`` uses the IMDS endpoint, which is
    NOT reachable in Azure Container Apps, so its login times out (HYT00). We hand
    pyodbc a clean connection string and inject the token via ``do_connect``.
    A URL already using ``odbc_connect`` is passed through untouched.
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
    parts.append("Encrypt=yes")
    for key, val in u.query.items():
        if key.lower() not in _AUTH_KEYS:  # drop driver + every auth keyword
            parts.append(f"{key}={first(val)}")
    return URL.create("mssql+pyodbc", query={"odbc_connect": ";".join(parts)})


def _attach_aad_token(engine: Engine) -> None:
    """Inject an Entra access token on each connect.

    Uses ``DefaultAzureCredential`` so it works with the Container Apps identity
    endpoint in Azure (selecting the user-assigned identity via ``AZURE_CLIENT_ID``)
    and ``az login`` locally -- where the driver's built-in MSI flow does not.
    ``azure-identity`` is imported lazily (it ships in the optional ``azure`` extra).
    """
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    @event.listens_for(engine, "do_connect")
    def _provide_token(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]  # noqa: ARG001
        raw = credential.get_token(_DB_TOKEN_SCOPE).token.encode("utf-16-le")
        cparams["attrs_before"] = {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack("<i", len(raw)) + raw}


def make_mssql_engine(url: str, **kwargs: object) -> Engine:
    """Azure SQL engine authenticated by a managed-identity access token.

    Shared by ``get_engine`` AND Alembic's ``env.py`` so the startup migration and
    the screen's writes connect to Azure SQL identically.
    """
    engine = create_engine(_mssql_odbc_url(url), **kwargs)
    _attach_aad_token(engine)
    return engine


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
        # serverless auto-pause/resume.
        return make_mssql_engine(url, pool_pre_ping=True, pool_recycle=3600)
    if url.endswith(":memory:"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine
