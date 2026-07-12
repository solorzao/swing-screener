"""add override stamp to trades (how a fill deviated from the engine's plan)

Revision ID: e4b8a2d6f1c9
Revises: d7e4b2f9a1c6
Create Date: 2026-07-11

The cockpit's log-trade action prefills from a Signal but never blocks Oliver from
taking the trade HIS way -- overriding the engine is allowed, hiding the override is
not. When the logged entry leaves the signal's zone (expressed in zone-R:
risk = entry_ceiling - stop) or the stop/target move (in %), the deviation is stamped
here verbatim ("entry +0.50R above ceiling; stop moved +1.1%") so the trade forever
carries what the engine planned vs what actually happened -- the learning loop and any
later post-mortem read the disagreement instead of losing it. Nullable, NO
server_default: NULL means engine-faithful or unprefilled (no signal to verify
against; the signal_id FK tells those apart), and every pre-cockpit row backfills to
that honest "nothing recorded" state. Bounded String(256) like the sibling notes
column (Azure SQL renders VARCHAR(n), never (max)); not indexed -- it is read
per-trade, never sliced.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "e4b8a2d6f1c9"
down_revision: Union[str, Sequence[str], None] = "d7e4b2f9a1c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("override", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "override")
