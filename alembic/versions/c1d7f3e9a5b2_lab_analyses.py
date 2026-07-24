"""add lab_analyses (TICKER LAB deep-analysis notes)

Revision ID: c1d7f3e9a5b2
Revises: f3c9a1e6b4d2
Create Date: 2026-07-18

The cockpit's TICKER LAB screen queues one row per on-demand deep-analysis
request and an in-process cockpit thread drains it immediately. The full
markdown report is stored inline (Text) so the cockpit renders it in place --
unlike analysis_requests, which stores a bounded summary plus blob keys.
``ticker`` and ``status`` are indexed like the analysis_requests precedent.
Local sqlite gets this table for free via get_engine's create_all; this
migration is the Azure SQL path.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1d7f3e9a5b2"
down_revision: str | Sequence[str] | None = "f3c9a1e6b4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("reasoning", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("is_deep", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("report", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column("est_cost_usd", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_lab_analyses_ticker"), "lab_analyses", ["ticker"],
                    unique=False)
    op.create_index(op.f("ix_lab_analyses_status"), "lab_analyses", ["status"],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_lab_analyses_status"), table_name="lab_analyses")
    op.drop_index(op.f("ix_lab_analyses_ticker"), table_name="lab_analyses")
    op.drop_table("lab_analyses")
