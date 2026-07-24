"""add would_surface tag to paper_trades (grade only what the digest would show)

Revision ID: b6e2d9f4a1c7
Revises: a8d3c5f1e7b2
Create Date: 2026-07-03

North Star #7 ("measure only what I'd actually trade"): reflection used to grade the
ENTIRE forward book -- ~92% hidden EARLY reversals plus rank-6+ names the digest never
shows -- and those verdicts then set conviction for the picks that ARE surfaced. Each
booked signal is now stamped with a booking-time estimate of whether it would surface
(strength + top-5 rank under the live surfacing config); reflection grades only the
stamped-True facet as gold. Nullable: NULL = legacy row (pre-stamping), never graded
as gold. Not indexed, matching the other denormalized tag columns.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6e2d9f4a1c7"
down_revision: str | Sequence[str] | None = "a8d3c5f1e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_trades", sa.Column("would_surface", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "would_surface")
