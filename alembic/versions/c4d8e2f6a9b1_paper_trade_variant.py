"""add screen-variant tag to paper_trades (strategy leaderboard)

Revision ID: c4d8e2f6a9b1
Revises: e9c7b3a15d24
Create Date: 2026-06-20

The shadow book gains a second, orthogonal experiment dimension. ``arm`` varies the
EXIT policy on a shared fill; ``variant`` varies the ENTRY/screen config, so each
variant re-screens the prior bar and paper-trades its own fills. breakdown(trades,
"variant") (filtered to the baseline exit arm) is the strategy leaderboard, the
complement of the per-arm exit A/B. server_default "default" backfills existing rows
to the live screen config; indexed because QC slices the book by variant.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d8e2f6a9b1"
down_revision: str | Sequence[str] | None = "e9c7b3a15d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("variant", sa.String(length=32),
                                            nullable=False, server_default="default"))
    op.create_index("ix_paper_trades_variant", "paper_trades", ["variant"])


def downgrade() -> None:
    op.drop_index("ix_paper_trades_variant", table_name="paper_trades")
    op.drop_column("paper_trades", "variant")
