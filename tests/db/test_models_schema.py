"""Schema-level assertions guarding Azure SQL portability.

A length-less ``Mapped[str]`` maps to NVARCHAR(max) on SQL Server, and Azure
SQL cannot index NVARCHAR(max). These tests pin explicit ``String`` lengths on
every string column (so the indexed ``ticker`` columns stay indexable) and
assert the EmailLog dedup uniqueness constraint. All checks are pure metadata
inspection -- no engine, no network.
"""

from typing import Any

from sqlalchemy import String, Table, UniqueConstraint, select
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


def _len(col: Any) -> int | None:
    """Bounded length of a String column.

    SQLAlchemy types ``column.type`` as ``TypeEngine[Any]`` (which has no
    ``.length``), so a direct ``col.type.length`` is a mypy attr-defined error
    though correct at runtime. The isinstance narrows it to ``String`` for a
    clean, checked access -- keeping these introspection tests mypy-clean.
    """
    t = col.type
    assert isinstance(t, String), f"expected a String column, got {type(t).__name__}"
    return t.length


def _unique_constraints(table: Any, name: str) -> list[UniqueConstraint]:
    """Named ``UniqueConstraint``s on a table.

    ``Model.__table__`` is typed as ``FromClause`` (no ``.constraints``); accept
    it as ``Any`` here so the runtime-correct introspection stays mypy-clean.
    """
    return [
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name == name
    ]


def _table(model: Any) -> Table:
    """A model's ``Table`` (``Model.__table__`` is typed ``FromClause`` -- narrow it
    so ``CreateTable`` and other ``Table``-typed APIs stay mypy-clean)."""
    return model.__table__


def test_ticker_columns_bounded_to_16() -> None:
    assert _len(Signal.__table__.c.ticker) == 16
    assert _len(Trade.__table__.c.ticker) == 16
    assert _len(PaperTrade.__table__.c.ticker) == 16
    assert _len(Universe.__table__.c.ticker) == 16
    # analyst_calls.ticker is indexed, so it must stay bounded (Azure SQL can't
    # index NVARCHAR(max)).
    assert _len(AnalystCall.__table__.c.ticker) == 16
    # execution_logs.ticker is indexed too -- same constraint.
    assert _len(ExecutionLog.__table__.c.ticker) == 16


def test_analyst_call_string_lengths() -> None:
    assert _len(AnalystCall.__table__.c.timeframe) == 32
    assert _len(AnalystCall.__table__.c.play_type) == 16
    assert _len(AnalystCall.__table__.c.baseline_conviction) == 16
    assert _len(AnalystCall.__table__.c.final_conviction) == 16
    assert _len(AnalystCall.__table__.c.nudge_reason) == 512
    assert _len(AnalystCall.__table__.c.model) == 64


def test_execution_log_string_lengths() -> None:
    assert _len(ExecutionLog.__table__.c.timeframe) == 32
    assert _len(ExecutionLog.__table__.c.play_type) == 16
    assert _len(ExecutionLog.__table__.c.account) == 16
    assert _len(ExecutionLog.__table__.c.mode) == 16
    assert _len(ExecutionLog.__table__.c.side) == 8
    assert _len(ExecutionLog.__table__.c.status) == 16
    assert _len(ExecutionLog.__table__.c.detail) == 512
    assert _len(ExecutionLog.__table__.c.idempotency_key) == 64
    # Phase 4 live-broker columns: broker + broker_order_id are indexed, so they
    # must stay bounded (Azure SQL can't index NVARCHAR(max)).
    assert _len(ExecutionLog.__table__.c.broker) == 16
    assert _len(ExecutionLog.__table__.c.broker_order_id) == 64
    assert _len(ExecutionLog.__table__.c.broker_status) == 32


def test_execution_log_idempotency_unique_constraint() -> None:
    matches = _unique_constraints(
        ExecutionLog.__table__, "uq_execution_logs_idempotency_key"
    )
    assert len(matches) == 1
    assert {c.name for c in matches[0].columns} == {"idempotency_key"}


def test_representative_string_lengths() -> None:
    assert _len(Signal.__table__.c.timeframe) == 32
    assert _len(Signal.__table__.c.chart_path) == 512
    assert _len(ExitEvent.__table__.c.message) == 256
    assert _len(EmailLog.__table__.c.kind) == 32
    # arm is indexed, so it must stay bounded (Azure SQL can't index NVARCHAR(max))
    assert _len(PaperTrade.__table__.c.arm) == 32


def test_email_log_has_alert_key_length_64() -> None:
    assert _len(EmailLog.__table__.c.alert_key) == 64


def test_email_log_dedup_unique_constraint() -> None:
    matches = _unique_constraints(EmailLog.__table__, "uq_email_log_dedup")
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
    signals_ddl = str(CreateTable(_table(Signal)).compile(dialect=MSDialect()))
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
