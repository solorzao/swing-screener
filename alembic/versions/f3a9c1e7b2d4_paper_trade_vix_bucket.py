"""add vix_bucket tag to paper_trades (VIX-rank regime attribution)

Revision ID: f3a9c1e7b2d4
Revises: 0c9f1070c373
Create Date: 2026-06-26

Each fill is stamped with the point-in-time VIX percentile-rank bucket at fill time
(trailing 252d): low/<40, mid/40-70, high/>70 -- so breakdown(trades, "vix_bucket") answers
whether the mean-reversion reversal book should be gated out of high-VIX panic states.
Nullable (NULL = unknown, e.g. ^VIX unavailable, or legacy rows); not indexed, matching the
other denormalized tag columns (market_trend / market_vol).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9c1e7b2d4"
down_revision: Union[str, Sequence[str], None] = "0c9f1070c373"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("vix_bucket", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "vix_bucket")
