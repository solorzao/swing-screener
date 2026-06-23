"""add sector to universe (GICS sector for the daily diversity cap)

Revision ID: 0c9f1070c373
Revises: bc856e48d4dc
Create Date: 2026-06-23

Adds ``Universe.sector``, populated from yfinance during the screen and read by the
daily digest's sector-diversity cap (keep <= N picks per sector). Nullable + additive:
existing rows backfill to NULL ("unknown" -> never capped, fail-open) until the next
screen populates them. Mirrors the Universe model in db/models.py.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0c9f1070c373"
down_revision: Union[str, Sequence[str], None] = "bc856e48d4dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("universe", sa.Column("sector", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("universe", "sector")
