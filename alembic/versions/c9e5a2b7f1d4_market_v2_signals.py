"""add v2 cross-asset signal columns to market_reports

Revision ID: c9e5a2b7f1d4
Revises: b8d4f1a6c3e2
Create Date: 2026-06-28

Persists the five v2 signals (VIX term structure, HY credit, cyclicals/defensives rotation,
equal-weight breadth, recession probability) on the weekly snapshot for history.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e5a2b7f1d4"
down_revision: str | Sequence[str] | None = "b8d4f1a6c3e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    ("vix_term_ratio", sa.Float()),
    ("vix_backwardation", sa.Boolean()),
    ("credit_chg_4w", sa.Float()),
    ("credit_pctile", sa.Float()),
    ("cyc_def_trend", sa.String(length=8)),
    ("cyc_def_chg_4w", sa.Float()),
    ("breadth_trend", sa.String(length=8)),
    ("breadth_chg_4w", sa.Float()),
    ("recession_prob", sa.Float()),
)


def upgrade() -> None:
    for name, col_type in _COLS:
        nullable = not isinstance(col_type, sa.Boolean)
        kwargs = {} if nullable else {"server_default": sa.false()}
        op.add_column("market_reports",
                      sa.Column(name, col_type, nullable=nullable, **kwargs))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("market_reports", name)
