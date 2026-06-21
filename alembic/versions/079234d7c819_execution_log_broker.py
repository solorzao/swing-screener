"""add live-broker tracking columns to execution_logs (Phase 4 live path)

Revision ID: 079234d7c819
Revises: 04c3f9c9f905
Create Date: 2026-06-21

Phase 4 adds a LIVE broker path. ``execution_logs`` gains three columns to track a real
broker order: ``broker`` (e.g. "alpaca"; "" for non-broker rows like manual / paper),
``broker_order_id``, and ``broker_status`` (the broker-reported status). ``broker`` carries
a server_default of "" so existing rows backfill to the non-broker empty string -- purely
additive, behavior-preserving (the new live statuses are handled in repo, not schema). The
two id columns are nullable (NULL until a live order is placed). ``broker`` +
``broker_order_id`` are indexed for per-broker / per-order lookups; every string column is
bounded so Azure SQL can index it (NVARCHAR(max) is un-indexable). Mirrors the ExecutionLog
model in db/models.py exactly.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "079234d7c819"
down_revision: Union[str, Sequence[str], None] = "04c3f9c9f905"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("execution_logs", sa.Column("broker", sa.String(length=16),
                                              nullable=False, server_default=""))
    op.add_column("execution_logs", sa.Column("broker_order_id", sa.String(length=64),
                                              nullable=True))
    op.add_column("execution_logs", sa.Column("broker_status", sa.String(length=32),
                                              nullable=True))
    op.create_index(op.f("ix_execution_logs_broker"), "execution_logs", ["broker"],
                    unique=False)
    op.create_index(op.f("ix_execution_logs_broker_order_id"), "execution_logs",
                    ["broker_order_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_logs_broker_order_id"), table_name="execution_logs")
    op.drop_index(op.f("ix_execution_logs_broker"), table_name="execution_logs")
    op.drop_column("execution_logs", "broker_status")
    op.drop_column("execution_logs", "broker_order_id")
    op.drop_column("execution_logs", "broker")
