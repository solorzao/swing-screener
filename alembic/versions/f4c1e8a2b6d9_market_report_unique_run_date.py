"""dedupe market_reports + unique index on run_date (idempotency backstop)

Revision ID: f4c1e8a2b6d9
Revises: e2b7a9d4c1f8
Create Date: 2026-07-02

market_run had no idempotency guard: the Sunday UTC cron pair double-fired and produced
two identical rows (+ two emails) for run_date 2026-06-26. run_market_report now checks
for an existing row before analyzing/emailing; this unique index is the backstop for the
concurrent-replica race. Existing duplicates are collapsed first (keeping the earliest
row per run_date) so the index can be created on a table that already carries the dupes.
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "f4c1e8a2b6d9"
down_revision: Union[str, Sequence[str], None] = "e2b7a9d4c1f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # collapse duplicates, keeping the earliest (lowest-id) row per run_date -- valid on
    # both SQLite and SQL Server.
    op.execute(
        "DELETE FROM market_reports WHERE id NOT IN "
        "(SELECT MIN(id) FROM market_reports GROUP BY run_date)"
    )
    op.create_index("uq_market_reports_run_date", "market_reports", ["run_date"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_market_reports_run_date", table_name="market_reports")
