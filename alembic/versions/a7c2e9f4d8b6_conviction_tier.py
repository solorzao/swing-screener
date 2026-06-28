"""add conviction_tier to signals and paper_trades (tiered surfacing + sizing)

Revision ID: a7c2e9f4d8b6
Revises: f3a9c1e7b2d4
Create Date: 2026-06-26

The reversal conviction tier (premium = high-vol bounce AND Wyckoff spring; strong = any single
conviction; base = none) drives tiered digest surfacing and conviction sizing. Denormalized onto
both signals (surfacing filter) and paper_trades (size-weighted performance). NOT NULL with a
"base" default so legacy rows read as the lowest tier; not indexed, matching the other tag columns.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "a7c2e9f4d8b6"
down_revision: Union[str, Sequence[str], None] = "f3a9c1e7b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("conviction_tier", sa.String(length=16),
                                       nullable=False, server_default="base"))
    op.add_column("paper_trades", sa.Column("conviction_tier", sa.String(length=16),
                                            nullable=False, server_default="base"))


def downgrade() -> None:
    op.drop_column("paper_trades", "conviction_tier")
    op.drop_column("signals", "conviction_tier")
