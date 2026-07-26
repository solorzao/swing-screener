"""Engine/session helpers for the screener's SQLite (later Azure SQL) store."""

import os
import struct

from sqlalchemy import URL, Engine, create_engine, event, make_url
from sqlalchemy.pool import StaticPool

from swing_screener.db.models import Base

# pyodbc: SQL_COPT_SS_ACCESS_TOKEN -- pass an Entra access token via attrs_before.
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_DB_TOKEN_SCOPE = "https://database.windows.net/.default"
# ODBC keywords we strip from an mssql URL: auth is via the access token instead.
_AUTH_KEYS = {"driver", "authentication", "uid", "user id", "pwd", "password", "trusted_connection"}


def _available_odbc_drivers() -> list[str]:
    """Installed ODBC drivers, or [] if pyodbc is unavailable (optional azure extra)."""
    try:
        import pyodbc
    except Exception:  # noqa: BLE001 -- optional azure extra; any import failure means no drivers
        return []
    return list(pyodbc.drivers())


def _best_sql_server_driver(available: list[str]) -> str | None:
    """Highest-numbered 'ODBC Driver NN for SQL Server' among `available`, else None."""
    def version(name: str) -> int:
        for tok in name.split():
            if tok.isdigit():
                return int(tok)
        return -1
    cands = [d for d in available
             if d.lower().startswith("odbc driver") and d.lower().endswith("for sql server")]
    return max(cands, key=version) if cands else None


def _resolve_driver(requested: str, available: list[str]) -> str:
    """Use `requested` if installed; else the best installed SQL Server driver; else `requested`."""
    if requested in available:
        return requested
    return _best_sql_server_driver(available) or requested


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

    requested = first(u.query.get("driver", "ODBC Driver 18 for SQL Server"))
    driver = _resolve_driver(requested, _available_odbc_drivers())
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


def _token_attrs(token: str) -> dict[int, bytes]:
    """The pyodbc ``attrs_before`` payload for one Entra access token: UTF-16-LE
    bytes, little-endian length-prefixed, under SQL_COPT_SS_ACCESS_TOKEN."""
    raw = token.encode("utf-16-le")
    return {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack("<i", len(raw)) + raw}


def _attach_aad_token(engine: Engine) -> None:
    """Inject an Entra access token on each connect.

    A PRE-FETCHED token in ``SWING_DB_ACCESS_TOKEN`` wins outright: CI's federated
    (GitHub-OIDC) assertion dies ~5 minutes after ``az login``, long before a long
    fetch-then-connect job reaches the database, so the workflow acquires the DB
    token while the assertion is alive and hands it over whole (2026-07-04:
    AADSTS700024 killed two reflect runs; az's own token cache keys on the exact
    audience string, so warming it is not deterministic). Tokens live ~60-75 min --
    fine for a single job, never for a resident service.

    Otherwise ``DefaultAzureCredential``, which works with the Container Apps
    identity endpoint in Azure (selecting the user-assigned identity via
    ``AZURE_CLIENT_ID``) and ``az login`` locally -- where the driver's built-in MSI
    flow does not. ``azure-identity`` is imported lazily (optional ``azure`` extra).
    """
    static_token = os.environ.get("SWING_DB_ACCESS_TOKEN")
    if static_token:
        @event.listens_for(engine, "do_connect")
        def _provide_static_token(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
            cparams["attrs_before"] = _token_attrs(static_token)
        return

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    @event.listens_for(engine, "do_connect")
    def _provide_token(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
        cparams["attrs_before"] = _token_attrs(credential.get_token(_DB_TOKEN_SCOPE).token)


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
