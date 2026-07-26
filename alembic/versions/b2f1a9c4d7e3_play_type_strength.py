"""add play_type + strength to signals and paper_trades (reversal screener)

Revision ID: b2f1a9c4d7e3
Revises: 16c775043cc8
Create Date: 2026-06-15

``play_type`` discriminates continuation (the pullback engine) from reversal (the
oversold-bounce engine); ``strength`` tags reversal plays early/confirmed. Both
columns are denormalized onto paper_trades so the shadow book is sliceable by
play_type without a join. NOT NULL play_type with a server_default backfills
existing rows to "continuation".
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2f1a9c4d7e3"
down_revision: str | Sequence[str] | None = "16c775043cc8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("play_type", sa.String(length=16),
                                       nullable=False, server_default="continuation"))
    op.add_column("signals", sa.Column("strength", sa.String(length=16), nullable=True))
    op.create_index(op.f("ix_signals_play_type"), "signals", ["play_type"], unique=False)

    op.add_column("paper_trades", sa.Column("play_type", sa.String(length=16),
                                            nullable=False, server_default="continuation"))
    op.add_column("paper_trades", sa.Column("strength", sa.String(length=16), nullable=True))
    op.create_index(op.f("ix_paper_trades_play_type"), "paper_trades", ["play_type"],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_paper_trades_play_type"), table_name="paper_trades")
    op.drop_column("paper_trades", "strength")
    op.drop_column("paper_trades", "play_type")
    op.drop_index(op.f("ix_signals_play_type"), table_name="signals")
    op.drop_column("signals", "strength")
    op.drop_column("signals", "play_type")
