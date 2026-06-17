"""add fractional-close fields to paper_trades (partial leg + ratcheting trail)

Revision ID: d3e8f1b6a2c9
Revises: cb4445986710
Create Date: 2026-06-16

Step B of the target/exit overhaul: the shadow book scales a fraction out at the
first target and trails the runner. ``partial_done``/``partial_price``/``partial_r``
record the booked first leg; ``remaining_frac`` is the runner's size (1.0 until a
partial, then e.g. 0.67); ``high_water`` is the highest high since the fill (the
trail reference). NOT NULL booleans/floats carry server_defaults so existing rows
backfill to the feature-off state (no partial, full size).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "d3e8f1b6a2c9"
down_revision: Union[str, Sequence[str], None] = "cb4445986710"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("partial_done", sa.Boolean(),
                                            nullable=False, server_default=sa.false()))
    op.add_column("paper_trades", sa.Column("partial_price", sa.Float(), nullable=True))
    op.add_column("paper_trades", sa.Column("partial_r", sa.Float(), nullable=True))
    op.add_column("paper_trades", sa.Column("remaining_frac", sa.Float(),
                                            nullable=False, server_default="1.0"))
    op.add_column("paper_trades", sa.Column("high_water", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "high_water")
    op.drop_column("paper_trades", "remaining_frac")
    op.drop_column("paper_trades", "partial_r")
    op.drop_column("paper_trades", "partial_price")
    op.drop_column("paper_trades", "partial_done")
