"""add play_type + autograde_json to option_setups (GEX checklist auto-grader)

Revision ID: f3c9a1e6b4d2
Revises: b4e9f2c7a3d1
Create Date: 2026-07-18

The GEX checklist auto-grader (docs/plans/2026-07-18-gex-checklist-autograde-design.md)
gives option_setups two columns:

* ``play_type`` -- breakout | range, the play chk_regime_match grades against. NOT
  NULL with a server_default of "" so existing rows backfill to the empty/legacy
  string (a setup journaled before the auto-grader has no declared play type).
* ``autograde_json`` -- the machine per-item verdicts / facts / threshold values
  captured at decision time, the provenance facet for later machine-vs-human
  discipline stats. NULLABLE Text, NO server_default / backfill: NULL means no
  auto-grade ran (a legacy row, or a setup graded by eye) -- never a fabricated $0
  grade. Text (never filtered/indexed), mirroring GexSnapshot.profile_json.

Purely additive, behavior-preserving. Mirrors the OptionSetup model in db/models.py
exactly. A create_all-born local.db already carries both columns but no
alembic_version -- ``alembic stamp head`` it first (existing convention: local
sqlite never self-migrates); this migration is for Azure SQL, where Alembic owns
the schema.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "f3c9a1e6b4d2"
down_revision: Union[str, Sequence[str], None] = "b4e9f2c7a3d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("option_setups", sa.Column("play_type", sa.String(length=16),
                                             nullable=False, server_default=""))
    op.add_column("option_setups", sa.Column("autograde_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("option_setups", "autograde_json")
    op.drop_column("option_setups", "play_type")
