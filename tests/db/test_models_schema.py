"""Schema-level assertions guarding Azure SQL portability.

A length-less ``Mapped[str]`` maps to NVARCHAR(max) on SQL Server, and Azure
SQL cannot index NVARCHAR(max). These tests pin explicit ``String`` lengths on
every string column (so the indexed ``ticker`` columns stay indexable) and
assert the EmailLog dedup uniqueness constraint. All checks are pure metadata
inspection -- no engine, no network.
"""

from sqlalchemy import UniqueConstraint, select
from sqlalchemy.dialects.mssql.base import MSDialect
from sqlalchemy.schema import CreateTable

from swing_screener.db.models import (
    AnalystCall,
    Base,
    EmailLog,
    ExecutionLog,
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
    # analyst_calls.ticker is indexed, so it must stay bounded (Azure SQL can't
    # index NVARCHAR(max)).
    assert AnalystCall.__table__.c.ticker.type.length == 16
    # execution_logs.ticker is indexed too -- same constraint.
    assert ExecutionLog.__table__.c.ticker.type.length == 16


def test_analyst_call_string_lengths() -> None:
    assert AnalystCall.__table__.c.timeframe.type.length == 32
    assert AnalystCall.__table__.c.play_type.type.length == 16
    assert AnalystCall.__table__.c.baseline_conviction.type.length == 16
    assert AnalystCall.__table__.c.final_conviction.type.length == 16
    assert AnalystCall.__table__.c.nudge_reason.type.length == 512
    assert AnalystCall.__table__.c.model.type.length == 64


def test_execution_log_string_lengths() -> None:
    assert ExecutionLog.__table__.c.timeframe.type.length == 32
    assert ExecutionLog.__table__.c.play_type.type.length == 16
    assert ExecutionLog.__table__.c.account.type.length == 16
    assert ExecutionLog.__table__.c.mode.type.length == 16
    assert ExecutionLog.__table__.c.side.type.length == 8
    assert ExecutionLog.__table__.c.status.type.length == 16
    assert ExecutionLog.__table__.c.detail.type.length == 512
    assert ExecutionLog.__table__.c.idempotency_key.type.length == 64


def test_execution_log_idempotency_unique_constraint() -> None:
    matches = [
        c
        for c in ExecutionLog.__table__.constraints
        if isinstance(c, UniqueConstraint)
        and c.name == "uq_execution_logs_idempotency_key"
    ]
    assert len(matches) == 1
    assert {c.name for c in matches[0].columns} == {"idempotency_key"}


def test_representative_string_lengths() -> None:
    assert Signal.__table__.c.timeframe.type.length == 32
    assert Signal.__table__.c.chart_path.type.length == 512
    assert ExitEvent.__table__.c.message.type.length == 256
    assert EmailLog.__table__.c.kind.type.length == 32
    # arm is indexed, so it must stay bounded (Azure SQL can't index NVARCHAR(max))
    assert PaperTrade.__table__.c.arm.type.length == 32


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


def test_boolean_filter_renders_mssql_safe() -> None:
    # Boolean filters must use `== True/False` (renders `= 1/0`), NOT `.is_(...)`
    # which renders `IS 0/1` -- valid on SQLite but a SQL Server syntax error
    # ("Incorrect syntax near '0'"). Guards pending_exit_alerts / exit_events_for.
    sql = str(
        select(ExitEvent)
        .where(ExitEvent.is_paper == False)  # noqa: E712
        .compile(dialect=MSDialect())
    )
    assert "IS 0" not in sql and "IS 1" not in sql
    assert "= 0" in sql
