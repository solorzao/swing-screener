"""add account dimension to paper_trades (isolate the intent book from the research grid)

Revision ID: 0a85303b1134
Revises: 388541d4b095
Create Date: 2026-06-21

Phase 3 fences the curated "intent" book off from the research grid. The shadow book
auto-books every screened signal x arm x variant under ``account = "research"``; a future
intent book paper-executes OrderIntents under ``account = "paper"``. The closed-trade
research aggregates (leaderboards + analyst calibration) read ``account = "research"`` so
the intent book never inflates them. The column carries a server_default of "research" so
existing rows backfill to the research grid -- purely additive, behavior-preserving.
Indexed because the book is sliced by account. Mirrors the PaperTrade model in
db/models.py exactly.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "0a85303b1134"
down_revision: Union[str, Sequence[str], None] = "388541d4b095"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("account", sa.String(length=16),
                                            nullable=False, server_default="research"))
    op.create_index(op.f("ix_paper_trades_account"), "paper_trades", ["account"])


def downgrade() -> None:
    op.drop_index(op.f("ix_paper_trades_account"), table_name="paper_trades")
    op.drop_column("paper_trades", "account")
