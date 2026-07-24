"""add execution_logs store (idempotency guard + audit trail + limit source)

Revision ID: ab19a164da8a
Revises: 0a85303b1134
Create Date: 2026-06-21

Phase 3's execution adapters share one append-only ``execution_logs`` table that does
quadruple duty: the IDEMPOTENCY guard (a unique ``idempotency_key`` per intent x run so a
force-resent or hourly-digest re-run never double-submits), the AUDIT trail, the SOURCE
for the per-day hard-limit sums (notional / loss), and the order ticket the ``manual``
adapter records. ``ticker`` is indexed for per-name lookups; every string column is
bounded so Azure SQL can index it (NVARCHAR(max) is un-indexable). Mirrors the
ExecutionLog model in db/models.py exactly.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ab19a164da8a"
down_revision: str | Sequence[str] | None = "0a85303b1134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=32), nullable=False),
        sa.Column("play_type", sa.String(length=16), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("account", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=False),
        sa.Column("shares", sa.Integer(), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("target", sa.Float(), nullable=False),
        sa.Column("risk_dollars", sa.Float(), nullable=False),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=512), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_logs_idempotency_key"),
    )
    op.create_index(op.f("ix_execution_logs_ticker"), "execution_logs", ["ticker"],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_logs_ticker"), table_name="execution_logs")
    op.drop_table("execution_logs")
