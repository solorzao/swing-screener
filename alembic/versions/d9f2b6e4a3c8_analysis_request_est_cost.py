"""add est_cost_usd to analysis_requests (meter the uncapped on-demand path)

Revision ID: d9f2b6e4a3c8
Revises: c6e2f4a8b1d3
Create Date: 2026-07-18

The on-demand single-ticker analyst (notify.ondemand) sends up to 4 chart images +
web search per request with NO spend cap, and a crash-requeue can re-bill the same
request -- yet analysis_requests carried zero cost visibility. analyze_ticker_deep
now returns a TickerAnalysis carrying the captured Usage (E3c) and the worker
persists its APPROXIMATE list-price estimate here. NULLABLE, no server_default /
backfill: NULL on failed/fallback/legacy rows (no billed call captured) -- an
honest unknown, never a fake $0. Purely additive, behavior-preserving. Mirrors the
AnalysisRequest model in db/models.py exactly.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9f2b6e4a3c8"
down_revision: str | Sequence[str] | None = "c6e2f4a8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_requests", sa.Column("est_cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_requests", "est_cost_usd")
