"""add trigger_ts to paper_trades (cross-run shadow-booking dedup)

Revision ID: e2b7a9d4c1f8
Revises: c9e5a2b7f1d4
Create Date: 2026-07-02

The timestamp of the completed bar a shadow candidate triggered on. A weekly flip bar is
re-detected as a "prior signal" on every daily run of the week (the prior frame drops only
the in-progress bucket), so without a trigger identity each weekly setup was booked 5-12
times, inflating the forward book with correlated pseudo-samples (2026-07-01 audit).
open_from_signals now skips candidates whose (ticker, timeframe, play_type, variant,
trigger_ts) is already booked. Nullable: legacy rows and test-seam candidates carry NULL
(no dedup applied to them); not indexed -- the dedup lookup filters by ticker (indexed)
plus an IN over the run's trigger timestamps.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "e2b7a9d4c1f8"
down_revision: Union[str, Sequence[str], None] = "c9e5a2b7f1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("trigger_ts", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "trigger_ts")
