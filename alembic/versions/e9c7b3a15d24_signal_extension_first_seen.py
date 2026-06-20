"""add extension_atr + first_seen_date to signals (freshness / staleness)

Revision ID: e9c7b3a15d24
Revises: f7a1c2d4e5b8
Create Date: 2026-06-20

``extension_atr`` records how far a continuation trigger's close sat above EMA20 in
ATR units (the anti-chase metric; NULL for reversal plays and legacy rows).
``first_seen_date`` is the streak-start run_date for a (ticker, timeframe, play_type)
setup -- the earliest of the consecutive runs it has been firing -- so the surface can
age out repeats. Both are nullable; existing rows backfill to NULL (treated as fresh).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "e9c7b3a15d24"
down_revision: Union[str, Sequence[str], None] = "f7a1c2d4e5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("extension_atr", sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("first_seen_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("signals", "first_seen_date")
    op.drop_column("signals", "extension_atr")
