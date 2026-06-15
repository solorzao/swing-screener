"""Schema-level assertions guarding Azure SQL portability.

A length-less ``Mapped[str]`` maps to NVARCHAR(max) on SQL Server, and Azure
SQL cannot index NVARCHAR(max). These tests pin explicit ``String`` lengths on
every string column (so the indexed ``ticker`` columns stay indexable) and
assert the EmailLog dedup uniqueness constraint. All checks are pure metadata
inspection -- no engine, no network.
"""

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.mssql.base import MSDialect
from sqlalchemy.schema import CreateTable

from swing_screener.db.models import (
    Base,
    EmailLog,
    ExitEvent,
    PaperTrade,
    Signal,
    Trade,
    Universe,
)


def test_ticker_columns_bounded_to_16() -> None:
    assert Signal.__table__.c.ticker.type.length == 16
    assert Trade.__table__.c.ticker.type.length == 16
    assert PaperTrade.__table__.c.ticker.type.length == 16
    assert Universe.__table__.c.ticker.type.length == 16


def test_representative_string_lengths() -> None:
    assert Signal.__table__.c.timeframe.type.length == 32
    assert Signal.__table__.c.chart_path.type.length == 512
    assert ExitEvent.__table__.c.message.type.length == 256
    assert EmailLog.__table__.c.kind.type.length == 32


def test_email_log_has_alert_key_length_64() -> None:
    assert EmailLog.__table__.c.alert_key.type.length == 64


def test_email_log_dedup_unique_constraint() -> None:
    matches = [
        c
        for c in EmailLog.__table__.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_email_log_dedup"
    ]
    assert len(matches) == 1
    uc = matches[0]
    assert {c.name for c in uc.columns} == {"kind", "run_date", "alert_key"}


def test_mssql_ddl_is_bounded_and_uses_bit() -> None:
    # The reason the columns are bounded: on the mssql dialect every string column
    # must render with an explicit length (VARCHAR(n), never (max)) so the indexed
    # ticker columns stay indexable, and booleans render as BIT. Compiles DDL
    # driverlessly (no pyodbc / no DB), so it runs in CI.
    dialect = MSDialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).lower()
        assert "(max)" not in ddl, f"{table.name} renders an unbounded string on mssql"
    signals_ddl = str(CreateTable(Signal.__table__).compile(dialect=MSDialect()))
    assert "BIT" in signals_ddl  # Boolean -> BIT
