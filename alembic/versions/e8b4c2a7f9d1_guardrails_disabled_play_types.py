"""agent_guardrails.disabled_play_types (the cockpit's tighten-only scope subtraction)

Revision ID: e8b4c2a7f9d1
Revises: a2e6c9f4b1d7
Create Date: 2026-07-25

The Strategy Board's half of the two-level execution scope
(docs/plans/2026-07-18-agent-guardrails-design.md, Addendum):

* ``SWING_EXECUTE_PLAY_TYPES`` (env) is the CEILING -- changing it is an env/IaC act.
* ``agent_guardrails.disabled_play_types`` (this column) is the cockpit SUBTRACTION:
  a comma-separated list of play types one click has disabled. Effective scope =
  ceiling - disabled, so the board can only ever remove risk.

NOT NULL with ``server_default=""``: the table already holds the single live brake
row, and an ALTER adding a NOT NULL column to a populated table needs a DEFAULT on
Azure SQL (sqlite would accept it either way -- the backend that matters is the one
that would fail). The default is kept on the column (not dropped after backfill) so
the model's ``server_default=""`` and the migrated schema stay byte-identical.

Mirrors ``AgentGuardrails.disabled_play_types`` in db/models.py exactly. Local sqlite
gets the column via get_engine's create_all; this migration is the Azure SQL path. A
create_all-born local.db already carries the schema but no alembic_version -- ``alembic
stamp head`` it first (existing convention: local sqlite never self-migrates).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "e8b4c2a7f9d1"
down_revision: Union[str, Sequence[str], None] = "a2e6c9f4b1d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_guardrails",
        sa.Column("disabled_play_types", sa.String(length=64), nullable=False,
                  server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_guardrails", "disabled_play_types")
