"""Guard: boolean-column filters must render for SQL Server, not just SQLite.

SQL Server's `IS` only accepts NULL, so `col IS 1` (what `.is_(True)` compiles to) is a
syntax error there -- but SQLite accepts it, so the unit tests (in-memory SQLite) pass
while prod (Azure SQL) crashes. This compiles the Journal v2 boolean filters against the
mssql dialect and asserts the portable `= 1` form, catching a `.is_(True)` regression.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects import mssql

from swing_screener.db.models import ExitEvent, SystemAudit


def _mssql(stmt) -> str:
    return str(stmt.compile(dialect=mssql.dialect()))


def test_is_paper_filter_renders_equals_not_is():
    sql = _mssql(select(ExitEvent.id).where(ExitEvent.is_paper == True))  # noqa: E712
    assert " IS 1" not in sql and " IS 0" not in sql
    assert "is_paper = 1" in sql


def test_acknowledged_filter_renders_equals_not_is():
    sql = _mssql(
        select(func.count(SystemAudit.id))
        .where(SystemAudit.acknowledged_by_human == True)  # noqa: E712
    )
    assert " IS 1" not in sql and " IS 0" not in sql
    assert "acknowledged_by_human = 1" in sql
