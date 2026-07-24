"""journal v2 tables (reviews, audits, weaknesses, disarm) + trades.emotional_state

Revision ID: b5f8d2a1c3e7
Revises: a4e7c1b9f2d6
Create Date: 2026-07-12

Journal v2 adds the two coaches' artifact tables. ``journal_reviews`` (Personal Trade
Coach) and ``system_audits`` (System Behavior Auditor) each store a code-owned facts
JSON (authoritative) plus a nullable LLM ``narrative`` (advisory; NULL -> template) and
nullable token-spend columns (NULL on the no-LLM path). ``weaknesses_profiles`` is the
append-only living distillation; ``disarm_events`` persists disarms the Auditor watches.
``trades`` gains ``emotional_state`` (manual actions only; NULL = none).

``journal_reviews.identity_key`` is a single UNIQUE the writer computes ("trade_close:
{book}:{trade_id}" / "weekly_rollup:{book}:{from}:{to}") -- a composite over the nullable
window columns would not dedup because SQL treats NULLs as distinct. ``system_audits``
uses a composite unique over its NOT-NULL (kind, period_from, period_to, breach_key).
Strings stay bounded (Azure SQL cannot index NVARCHAR(max)); JSON bodies are Text.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5f8d2a1c3e7"
down_revision: str | Sequence[str] | None = "a4e7c1b9f2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "journal_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("identity_key", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("book", sa.String(length=16), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("covered_from", sa.Date(), nullable=True),
        sa.Column("covered_to", sa.Date(), nullable=True),
        sa.Column("facts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("human_edit", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("est_cost_usd", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_journal_reviews_identity", "journal_reviews", ["identity_key"], unique=True
    )
    op.create_index("ix_journal_reviews_book", "journal_reviews", ["book"])

    op.create_table(
        "system_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column("breach_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("findings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("acknowledged_by_human", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("est_cost_usd", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_system_audits_identity", "system_audits",
        ["kind", "period_from", "period_to", "breach_key"], unique=True,
    )

    op.create_table(
        "weaknesses_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="personal"),
        sa.Column("items_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("covered_from", sa.Date(), nullable=True),
        sa.Column("covered_to", sa.Date(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "disarm_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("orders_cancelled", sa.Integer(), nullable=False, server_default="0"),
    )

    op.add_column(
        "trades", sa.Column("emotional_state", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("trades", "emotional_state")
    op.drop_table("disarm_events")
    op.drop_table("weaknesses_profiles")
    op.drop_index("uq_system_audits_identity", table_name="system_audits")
    op.drop_table("system_audits")
    op.drop_index("ix_journal_reviews_book", table_name="journal_reviews")
    op.drop_index("uq_journal_reviews_identity", table_name="journal_reviews")
    op.drop_table("journal_reviews")
