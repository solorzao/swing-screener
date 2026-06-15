"""Alembic environment.

The screener's SQLAlchemy models own the schema, so autogenerate compares against
``Base.metadata`` and the DB URL comes from the same settings the app uses
(``SWING_DB_URL``) -- one source of truth for both the app and migrations. In
Azure the pipeline runs ``alembic upgrade head`` on startup against Azure SQL
(passwordless, via the URL's ``Authentication=ActiveDirectory...``); locally the
same command can target the dev SQL Server / SQLite.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from swing_screener.db.models import Base
from swing_screener.db.session import make_mssql_engine
from swing_screener.settings import load_settings

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: the pipeline runs `alembic upgrade head` on
    # startup (Task 8), and the default True would silently disable every app
    # logger configured before this point.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Point both the URL and autogenerate at the app's own config/metadata.
config.set_main_option("sqlalchemy.url", load_settings().db_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a DBAPI connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against the configured database.

    Azure SQL connects via ``make_mssql_engine`` (managed-identity access token,
    no Trusted_Connection/FA001 and no driver-side MSI/HYT00) -- the same path
    ``get_engine`` uses for the screen; other URLs use a plain engine.
    """
    url = load_settings().db_url
    connectable = (
        make_mssql_engine(url, poolclass=pool.NullPool)
        if url.startswith("mssql")
        else create_engine(url, poolclass=pool.NullPool)
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
