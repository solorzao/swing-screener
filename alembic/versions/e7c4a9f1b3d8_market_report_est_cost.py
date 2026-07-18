"""add est_cost_usd to market_reports (spend visibility for the weekly LLM read)

Revision ID: e7c4a9f1b3d8
Revises: d9f2b6e4a3c8
Create Date: 2026-07-18

The weekly Market Weather job makes ONE deep-analysis (web-search) call per run
with usage captured but never persisted -- zero spend visibility (2026-07-17
audit, E6). The MarketReport row now carries the APPROXIMATE list-price estimate
of that call. NULLABLE, no server_default / backfill: NULL on deterministic /
fallback / legacy rows (no billed call captured) -- an honest unknown, never a
fake $0. Purely additive, behavior-preserving. Mirrors the MarketReport model in
db/models.py exactly.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "e7c4a9f1b3d8"
down_revision: Union[str, Sequence[str], None] = "d9f2b6e4a3c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("market_reports", sa.Column("est_cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("market_reports", "est_cost_usd")
