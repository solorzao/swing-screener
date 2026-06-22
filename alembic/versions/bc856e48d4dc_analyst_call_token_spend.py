"""add token-spend columns to analyst_calls (capture + persist Opus cost)

Revision ID: bc856e48d4dc
Revises: 079234d7c819
Create Date: 2026-06-21

To run the insight engine production-on we need to see what each Opus analyst call
costs. The Messages API already returns ``resp.usage`` (token counts); we now capture
it (``notify.analysis.Usage``) and persist the four facets here: the raw input/output
token counts, the web-search count, and an APPROXIMATE list-price cost estimate that
feeds the Task-2 run-cost safety cap. All four are NULLABLE -- they are NULL on the
deterministic/fallback path (no model call was made) and on legacy rows, so no
server_default / backfill is needed. Purely additive, behavior-preserving. Mirrors the
AnalystCall model in db/models.py exactly.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "bc856e48d4dc"
down_revision: Union[str, Sequence[str], None] = "079234d7c819"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analyst_calls", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("analyst_calls", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("analyst_calls", sa.Column("web_searches", sa.Integer(), nullable=True))
    op.add_column("analyst_calls", sa.Column("est_cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("analyst_calls", "est_cost_usd")
    op.drop_column("analyst_calls", "web_searches")
    op.drop_column("analyst_calls", "output_tokens")
    op.drop_column("analyst_calls", "input_tokens")
