"""agent guardrails tables + PaperTrade.qty (the live agent's brake state)

Revision ID: a2e6c9f4b1d7
Revises: c1d7f3e9a5b2
Create Date: 2026-07-18

The DB-backed brake the live agent reads before every order submit
(docs/plans/2026-07-18-agent-guardrails-implementation.md):

* ``agent_guardrails`` -- ONE mutable row (id=1 by convention) holding the brake
  state ('ok' | 'halted' | 'tripped' -- a String, never a boolean, so no WHERE
  clause renders `IS 1` on SQL Server), the four breaker thresholds (NULL =
  unset), the drawdown anchor/baseline, and trip bookkeeping.
* ``agent_guardrail_events`` -- append-only history + the Auditor feed: one row
  per edit / halt / trip / clear / sweep outcome, mirroring disarm_events.
* ``paper_trades.qty`` -- live-book share count stamped at fill materialization.
  NULLABLE, no backfill: NULL on every non-live book and on legacy live rows --
  realized $ math must skip NULL, never guess.

Mirrors the models in db/models.py exactly. Local sqlite gets these tables via
get_engine's create_all; this migration is the Azure SQL path. A create_all-born
local.db already carries the schema but no alembic_version -- ``alembic stamp
head`` it first (existing convention: local sqlite never self-migrates).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "a2e6c9f4b1d7"
down_revision: Union[str, Sequence[str], None] = "c1d7f3e9a5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_guardrails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("max_daily_loss_usd", sa.Float(), nullable=True),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=True),
        sa.Column("max_drawdown_usd", sa.Float(), nullable=True),
        sa.Column("loss_streak_halt", sa.Integer(), nullable=True),
        sa.Column("hwm_anchor_date", sa.Date(), nullable=True),
        sa.Column("hwm_baseline_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trip_id", sa.Integer(), nullable=True),
        sa.Column("trip_reason", sa.String(length=256), nullable=True),
        sa.Column("sweep_state", sa.String(length=16), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_guardrail_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("breaker", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("values_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("paper_trades", sa.Column("qty", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "qty")
    op.drop_table("agent_guardrail_events")
    op.drop_table("agent_guardrails")
