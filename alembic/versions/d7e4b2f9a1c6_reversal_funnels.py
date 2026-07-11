"""create reversal_funnels (one funnel snapshot per daily digest)

Revision ID: d7e4b2f9a1c6
Revises: c8f3a6d1e9b4
Create Date: 2026-07-10

The reversal funnel tuple (detected, confirmed, fresh, actionable, surfaced) is built
only during the daily digest and previously died with the email. fresh/actionable/
surfaced depend on digest-time state (repeat cooldown vs the signals history, live
quotes for the already-ran drop, sector cap) and CANNOT be recomputed later -- this
table is the only record; the cockpit's funnel view reads it. ONE row per run_date
(unique index): the write site delete-then-inserts so a forced digest resend
re-records instead of duplicating. Standalone table -- no FKs. Strings stay bounded
so Azure SQL can index them (NVARCHAR(max) is un-indexable).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "d7e4b2f9a1c6"
down_revision: Union[str, Sequence[str], None] = "c8f3a6d1e9b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reversal_funnels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("detected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fresh", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actionable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("surfaced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overflow_tickers", sa.String(length=512), nullable=False,
                  server_default=""),
        sa.Column("pool_n", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("confirmed_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("premium_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("already_ran_checked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("uq_reversal_funnels_run_date", "reversal_funnels", ["run_date"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("uq_reversal_funnels_run_date", table_name="reversal_funnels")
    op.drop_table("reversal_funnels")
