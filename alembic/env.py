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
from swing_screener.db.session import to_connect_url
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

    Build the engine via ``to_connect_url`` (not ``engine_from_config`` off the
    raw URL) so an Azure SQL managed-identity URL uses the ``odbc_connect`` form
    -- otherwise SQLAlchemy auto-adds Trusted_Connection=Yes and the connection
    dies with ODBC FA001 (the same fix ``get_engine`` applies for the screen).
    """
    connectable = create_engine(
        to_connect_url(load_settings().db_url), poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
