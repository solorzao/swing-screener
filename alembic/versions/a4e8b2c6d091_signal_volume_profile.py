"""add the setup volume profile to signals (rvol_trigger / rvol_pullback / pocket_pivot)

Revision ID: a4e8b2c6d091
Revises: c1d7f3e9a5b2
Create Date: 2026-08-16

The conviction analyst previously had no volume reading at all -- volume reached it
only as pixels inside the chart image. These three columns carry the setup's volume
footprint (signals.volume.volume_profile), measured at screen time because only the
pipeline holds the OHLCV frame; the digest reads persisted rows.

All three are NULLABLE and that is load-bearing: NULL means NOT MEASURED (a legacy
row, NaN volume -- index tickers keep those rows at the download seam -- or too
little history). They must never be backfilled with a neutral 1.0, which the analyst
would read as "volume was unremarkable" rather than "volume is unknown".

Local sqlite gets these for free via get_engine's create_all; this migration is the
Azure SQL path.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e8b2c6d091"
down_revision: str | Sequence[str] | None = "c1d7f3e9a5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("rvol_trigger", sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("rvol_pullback", sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("pocket_pivot", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("signals", "pocket_pivot")
    op.drop_column("signals", "rvol_pullback")
    op.drop_column("signals", "rvol_trigger")
