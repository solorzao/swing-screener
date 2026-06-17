"""add experiment-arm tag to paper_trades (parallel-arm dual-book)

Revision ID: f7a1c2d4e5b8
Revises: d3e8f1b6a2c9
Create Date: 2026-06-16

Step C of the target/exit overhaul: every fill is duplicated once per experiment
arm (same entry economics, different exit management), so the shadow book holds a
complete book per arm and breakdown(trades, "arm") gives a same-sample A/B. The
column carries a server_default of "baseline" so existing rows backfill to the
all-or-nothing arm. Indexed because QC slices the book by arm.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "f7a1c2d4e5b8"
down_revision: Union[str, Sequence[str], None] = "d3e8f1b6a2c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("arm", sa.String(length=32),
                                            nullable=False, server_default="baseline"))
    op.create_index("ix_paper_trades_arm", "paper_trades", ["arm"])


def downgrade() -> None:
    op.drop_index("ix_paper_trades_arm", table_name="paper_trades")
    op.drop_column("paper_trades", "arm")
