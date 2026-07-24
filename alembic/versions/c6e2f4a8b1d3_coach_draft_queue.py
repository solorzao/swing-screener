"""coach_draft_requests async queue (on-close narrative drafts)

Revision ID: c6e2f4a8b1d3
Revises: b5f8d2a1c3e7
Create Date: 2026-07-12

The synchronous cockpit close writes the JournalReview facts row and enqueues one of
these; the journal.coach_run worker drains it and backfills the review's narrative.
Mirrors analysis_requests (status queued|running|done|failed, started_at claim clock).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6e2f4a8b1d3"
down_revision: str | Sequence[str] | None = "b5f8d2a1c3e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coach_draft_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(length=1024), nullable=True),
    )
    op.create_index("ix_coach_draft_requests_review_id", "coach_draft_requests", ["review_id"])
    op.create_index("ix_coach_draft_requests_status", "coach_draft_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_coach_draft_requests_status", table_name="coach_draft_requests")
    op.drop_index("ix_coach_draft_requests_review_id", table_name="coach_draft_requests")
    op.drop_table("coach_draft_requests")
