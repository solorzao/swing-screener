"""add analyst_calls store (analyst conviction learning/calibration loop)

Revision ID: 388541d4b095
Revises: d5b9f3a72e16
Create Date: 2026-06-20

Each row records one analyst conviction call: the pick keys, the deterministic
BASELINE and the analyst's FINAL conviction, the nudge reason, and the model.
``realized_r`` / ``scored_at`` are nullable -- a freshly recorded call is UNSCORED
until a later pass grades how the nudge played out, closing the calibration loop.
``ticker`` is indexed for per-name lookups; every string column is bounded so
Azure SQL can index it (NVARCHAR(max) is un-indexable). Mirrors the AnalystCall
model in db/models.py exactly.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "388541d4b095"
down_revision: Union[str, Sequence[str], None] = "d5b9f3a72e16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analyst_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=32), nullable=False),
        sa.Column("play_type", sa.String(length=16), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("baseline_conviction", sa.String(length=16), nullable=False),
        sa.Column("final_conviction", sa.String(length=16), nullable=False),
        sa.Column("nudge_reason", sa.String(length=512), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("realized_r", sa.Float(), nullable=True),
        sa.Column("scored_at", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_analyst_calls_ticker"), "analyst_calls", ["ticker"],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_analyst_calls_ticker"), table_name="analyst_calls")
    op.drop_table("analyst_calls")
