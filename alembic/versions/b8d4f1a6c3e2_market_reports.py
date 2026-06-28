"""create market_reports (weekly macro Market Weather snapshots)

Revision ID: b8d4f1a6c3e2
Revises: a7c2e9f4d8b6
Create Date: 2026-06-26

One row per weekly market-screener run: the deterministic market facts (SPY HA alignment, VIX
regime, yield inversion, bond trend) plus the analyst's read. A history of how the market moved
and a flip log. Standalone table -- no FKs.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f1a6c3e2"
down_revision: Union[str, Sequence[str], None] = "a7c2e9f4d8b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_date", sa.Date(), nullable=False, index=True),
        sa.Column("ha_alignment", sa.String(length=16), nullable=False),
        sa.Column("flipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("spy_vs_200dma", sa.String(length=8), nullable=True),
        sa.Column("vol_bucket", sa.String(length=16), nullable=True),
        sa.Column("vix", sa.Float(), nullable=True),
        sa.Column("vix_rank", sa.Float(), nullable=True),
        sa.Column("vix_spike", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ten_year", sa.Float(), nullable=True),
        sa.Column("three_month", sa.Float(), nullable=True),
        sa.Column("yield_inverted", sa.Boolean(), nullable=True),
        sa.Column("bond_trend", sa.String(length=8), nullable=True),
        sa.Column("is_deep", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("core", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("report", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("market_reports")
