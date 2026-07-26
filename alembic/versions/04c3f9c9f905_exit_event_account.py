"""add account dimension to exit_events (separate research-grid exits from the intent book)

Revision ID: 04c3f9c9f905
Revises: ab19a164da8a
Create Date: 2026-06-21

The shadow stepper records EVERY paper exit under ``is_paper = True`` -- both the
research grid (``account = "research"``) and the curated intent book (``account =
"paper"``) -- so ``is_paper`` alone can't separate them. This column mirrors
``PaperTrade.account`` onto ``ExitEvent`` so the dashboard's exit log can be sliced by
book. It carries a server_default of "research" so existing rows backfill to the research
grid -- purely additive, display-only, behavior-preserving (no numeric aggregate reads
ExitEvent). Indexed because the log is sliced by account. Mirrors the ExitEvent model in
db/models.py exactly.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "04c3f9c9f905"
down_revision: str | Sequence[str] | None = "ab19a164da8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("exit_events", sa.Column("account", sa.String(length=16),
                                           nullable=False, server_default="research"))
    op.create_index(op.f("ix_exit_events_account"), "exit_events", ["account"])


def downgrade() -> None:
    op.drop_index(op.f("ix_exit_events_account"), table_name="exit_events")
    op.drop_column("exit_events", "account")
