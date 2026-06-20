"""add market-regime tags to paper_trades (regime attribution)

Revision ID: d5b9f3a72e16
Revises: c4d8e2f6a9b1
Create Date: 2026-06-20

Each fill is stamped with the broad market context it was taken in -- ``market_trend``
(SPY vs its 200-day SMA: bull/bear) and ``market_vol`` (SPY ATR% band: calm/elevated/
high) -- so breakdown(trades, "market_trend") tells us WHEN each engine works. Both are
nullable (NULL = unknown, e.g. SPY data unavailable, or legacy rows); not indexed, matching
the other denormalized tag columns (quality_tier / volatility_tier).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "d5b9f3a72e16"
down_revision: Union[str, Sequence[str], None] = "c4d8e2f6a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("market_trend", sa.String(length=16), nullable=True))
    op.add_column("paper_trades", sa.Column("market_vol", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "market_vol")
    op.drop_column("paper_trades", "market_trend")
