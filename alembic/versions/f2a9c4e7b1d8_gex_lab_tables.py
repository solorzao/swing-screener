"""create the four GEX options-lab tables

Revision ID: f2a9c4e7b1d8
Revises: e4b8a2d6f1c9
Create Date: 2026-07-12

The options lab (docs/modules/gex-lab.md) is a firewalled sibling module: it never
writes the equity tables, so it gets its own parallel family. SQLite gets these via
create_all; this migration is for Azure SQL, where Alembic owns the schema. Lifecycle
columns are DateTime, not Date -- the lab trades inside the session. Strings stay
bounded so Azure SQL can index them (NVARCHAR(max) is un-indexable); the per-strike
GEX profile is display-only Text. broker_fills.import_hash and
option_paper_trades.import_key are unique -- the two import idempotency keys.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "f2a9c4e7b1d8"
down_revision: Union[str, Sequence[str], None] = "e4b8a2d6f1c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gex_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("spot", sa.Float(), nullable=False),
        sa.Column("call_wall", sa.Float(), nullable=True),
        sa.Column("put_wall", sa.Float(), nullable=True),
        sa.Column("gamma_flip", sa.Float(), nullable=True),
        sa.Column("net_gex", sa.Float(), nullable=True),
        sa.Column("regime", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("thin_chain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="computed"),
    )
    op.create_index("ix_gex_snapshots_underlying", "gex_snapshots", ["underlying"])
    op.create_index("ix_gex_snapshots_ts", "gex_snapshots", ["ts"])

    op.create_table(
        "option_setups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("gex_snapshot_id", sa.Integer(),
                  sa.ForeignKey("gex_snapshots.id"), nullable=True),
        sa.Column("regime", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("pivot_level", sa.Float(), nullable=True),
        sa.Column("pattern", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("entry", sa.Float(), nullable=True),
        sa.Column("stop", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("chk_daily_bias_clear", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_daily_stack_ordered", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_m5_agrees", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chk_gex_levels_marked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_price_at_pivot", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_regime_match", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chk_pattern_clean", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_volume_confirming", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_risk_sized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("chk_stop_structural", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_rr_at_least_2", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("chk_confirmation_candle", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("grade", sa.String(length=8), nullable=False, server_default="no_trade"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="idea"),
        sa.Column("notes", sa.String(length=2048), nullable=False, server_default=""),
    )
    op.create_index("ix_option_setups_ts", "option_setups", ["ts"])
    op.create_index("ix_option_setups_underlying", "option_setups", ["underlying"])
    op.create_index("ix_option_setups_status", "option_setups", ["status"])

    op.create_table(
        "option_paper_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("setup_id", sa.Integer(), sa.ForeignKey("option_setups.id"), nullable=True),
        sa.Column("account", sa.String(length=16), nullable=False,
                  server_default="options-lab"),
        sa.Column("strategy", sa.String(length=16), nullable=False, server_default="gex"),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False, server_default="long"),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("entry", sa.Float(), nullable=True),
        sa.Column("stop", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(length=16), nullable=True),
        sa.Column("realized_r", sa.Float(), nullable=True),
        sa.Column("hold_minutes", sa.Integer(), nullable=True),
        sa.Column("occ_symbol", sa.String(length=24), nullable=True),
        sa.Column("strike", sa.Float(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("right", sa.String(length=4), nullable=True),
        sa.Column("contracts", sa.Integer(), nullable=True),
        sa.Column("entry_premium", sa.Float(), nullable=True),
        sa.Column("exit_premium", sa.Float(), nullable=True),
        sa.Column("premium_pnl", sa.Float(), nullable=True),
        sa.Column("import_key", sa.String(length=64), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_option_paper_trades_account", "option_paper_trades", ["account"])
    op.create_index("ix_option_paper_trades_strategy", "option_paper_trades", ["strategy"])
    op.create_index("ix_option_paper_trades_underlying", "option_paper_trades", ["underlying"])
    op.create_index("ix_option_paper_trades_status", "option_paper_trades", ["status"])
    op.create_index("ix_option_paper_trades_occ_symbol", "option_paper_trades", ["occ_symbol"])
    op.create_index("uq_option_paper_trades_import_key", "option_paper_trades",
                    ["import_key"], unique=True)

    op.create_table(
        "broker_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("import_hash", sa.String(length=64), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("underlying", sa.String(length=16), nullable=False),
        sa.Column("occ_symbol", sa.String(length=24), nullable=False),
        sa.Column("trans_code", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("raw", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="robinhood"),
    )
    op.create_index("uq_broker_fills_import_hash", "broker_fills", ["import_hash"], unique=True)
    op.create_index("ix_broker_fills_activity_date", "broker_fills", ["activity_date"])
    op.create_index("ix_broker_fills_underlying", "broker_fills", ["underlying"])
    op.create_index("ix_broker_fills_occ_symbol", "broker_fills", ["occ_symbol"])


def downgrade() -> None:
    op.drop_index("ix_broker_fills_occ_symbol", table_name="broker_fills")
    op.drop_index("ix_broker_fills_underlying", table_name="broker_fills")
    op.drop_index("ix_broker_fills_activity_date", table_name="broker_fills")
    op.drop_index("uq_broker_fills_import_hash", table_name="broker_fills")
    op.drop_table("broker_fills")

    op.drop_index("uq_option_paper_trades_import_key", table_name="option_paper_trades")
    op.drop_index("ix_option_paper_trades_occ_symbol", table_name="option_paper_trades")
    op.drop_index("ix_option_paper_trades_status", table_name="option_paper_trades")
    op.drop_index("ix_option_paper_trades_underlying", table_name="option_paper_trades")
    op.drop_index("ix_option_paper_trades_strategy", table_name="option_paper_trades")
    op.drop_index("ix_option_paper_trades_account", table_name="option_paper_trades")
    op.drop_table("option_paper_trades")

    op.drop_index("ix_option_setups_status", table_name="option_setups")
    op.drop_index("ix_option_setups_underlying", table_name="option_setups")
    op.drop_index("ix_option_setups_ts", table_name="option_setups")
    op.drop_table("option_setups")

    op.drop_index("ix_gex_snapshots_ts", table_name="gex_snapshots")
    op.drop_index("ix_gex_snapshots_underlying", table_name="gex_snapshots")
    op.drop_table("gex_snapshots")
