"""add pending-fill-window columns to paper_trades (reversal resting-limit entries)

Revision ID: a8d3c5f1e7b2
Revises: f4c1e8a2b6d9
Create Date: 2026-07-02

The 2026-07-01 audit found ~95% of confirmed reversals unfillable in the one-bar fill
window (the entry needs a >38% retrace of the bounce on the very next bar) and the few
fills adversely selected (live -0.86R vs replay +0.125R). A reversal entry is a resting
limit order: it now stays PENDING (fill_status/status "pending") for up to
``reversal_fill_window_bars`` completed bars. These columns persist the entry zone
(floor/ceiling -- the transient screen-time EntryZone is otherwise lost) and the
window counter. All nullable; legacy rows carry NULL (never pended).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8d3c5f1e7b2"
down_revision: str | Sequence[str] | None = "f4c1e8a2b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("entry_floor", sa.Float(), nullable=True))
    op.add_column("paper_trades", sa.Column("entry_ceiling", sa.Float(), nullable=True))
    op.add_column("paper_trades", sa.Column("pending_bars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "pending_bars")
    op.drop_column("paper_trades", "entry_ceiling")
    op.drop_column("paper_trades", "entry_floor")
