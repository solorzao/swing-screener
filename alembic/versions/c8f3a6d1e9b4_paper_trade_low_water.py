"""add low_water MAE tracking to paper_trades

Revision ID: c8f3a6d1e9b4
Revises: b6e2d9f4a1c7
Create Date: 2026-07-03

The lowest low since the fill, folded forward by the bar-stepper exactly like
high_water -- pure max-adverse-excursion instrumentation, never read by any exit
decision. With it, stop-width / breakeven-timing / target-reachability questions are
answerable offline from the accrued book instead of each burning weeks as a new arm.
Nullable (NULL = legacy row); not indexed, matching the other stepper-state columns.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "c8f3a6d1e9b4"
down_revision: Union[str, Sequence[str], None] = "b6e2d9f4a1c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("low_water", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "low_water")
