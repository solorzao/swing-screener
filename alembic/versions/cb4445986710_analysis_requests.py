"""add analysis_requests queue (on-demand deep-analysis)

Revision ID: cb4445986710
Revises: b2f1a9c4d7e3
Create Date: 2026-06-16

The on-demand analysis feature enqueues a single-ticker deep-analysis report per
row. ``status`` walks queued -> running -> done/failed; the worker claims rows by
flipping queued -> running under a single atomic UPDATE. ``ticker`` and ``status``
are indexed so the claim query (status='queued') and per-ticker lookups stay cheap.
String columns carry server-side defaults so a freshly INSERTed queued row needs
only ticker + requested_at.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "cb4445986710"
down_revision: Union[str, Sequence[str], None] = "b2f1a9c4d7e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analysis_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("recipient", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("pdf_blob_key", sa.String(length=512), nullable=True),
        sa.Column("chart_blob_keys", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_analysis_requests_ticker"), "analysis_requests", ["ticker"],
                    unique=False)
    op.create_index(op.f("ix_analysis_requests_status"), "analysis_requests", ["status"],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_analysis_requests_status"), table_name="analysis_requests")
    op.drop_index(op.f("ix_analysis_requests_ticker"), table_name="analysis_requests")
    op.drop_table("analysis_requests")
