"""create journal tables (tags, trade-tags, notes, theses) with source provenance

Revision ID: a4e7c1b9f2d6
Revises: e4b8a2d6f1c9
Create Date: 2026-07-12

The Journal layer v1 overlays three annotation tables on the existing paper-trade
book. Each carries a bounded ``source`` (screener | analyst | human) so every mark
knows its origin -- the shadow-contamination lesson, enforced as NOT NULL. The join
tables (journal_trade_tags, journal_theses) key on a plain (trade_id, book) pair, NOT
an FK into paper_trades: the journal is a decoupled read-model overlay, and ``book``
(the account) disambiguates the id across books. Only journal_trade_tags.tag_id is a
real FK -- into journal_tags, this migration's own vocabulary table. Strings stay
bounded so Azure SQL can index them (NVARCHAR(max) is un-indexable); bodies are Text.

The upgrade also seeds the mistake taxonomy (chased, moved_stop, oversized,
early_exit, no_setup, revenge) as ``kind="mistake"`` tags so the mistake-cost report
has a shared vocabulary from day one; the downgrade drops all four tables.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "a4e7c1b9f2d6"
down_revision: Union[str, Sequence[str], None] = "e4b8a2d6f1c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The seeded mistake vocabulary (kind="mistake"). Bulk-inserted at upgrade so the
# mistake-cost report groups against a stable set of names, not ad-hoc free text.
_MISTAKE_TAXONOMY = [
    ("chased", "entered too far above the setup / after the move"),
    ("moved_stop", "widened or walked the stop against the plan"),
    ("oversized", "position larger than the risk unit allowed"),
    ("early_exit", "closed before the plan's target / signal"),
    ("no_setup", "took a trade with no qualifying setup"),
    ("revenge", "re-entered to recover a prior loss"),
]


def upgrade() -> None:
    op.create_table(
        "journal_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False, server_default=""),
    )
    op.create_table(
        "journal_trade_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("book", sa.String(length=16), nullable=False),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("journal_tags.id"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_journal_trade_tags_trade_id", "journal_trade_tags", ["trade_id"])
    op.create_table(
        "journal_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("module", sa.String(length=16), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_journal_notes_day", "journal_notes", ["day"])
    op.create_table(
        "journal_theses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("book", sa.String(length=16), nullable=False),
        sa.Column("event_kind", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_journal_theses_trade_id", "journal_theses", ["trade_id"])

    journal_tags = sa.table(
        "journal_tags",
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(journal_tags, [
        {"kind": "mistake", "name": name, "description": desc}
        for name, desc in _MISTAKE_TAXONOMY
    ])


def downgrade() -> None:
    op.drop_index("ix_journal_theses_trade_id", table_name="journal_theses")
    op.drop_table("journal_theses")
    op.drop_index("ix_journal_notes_day", table_name="journal_notes")
    op.drop_table("journal_notes")
    op.drop_index("ix_journal_trade_tags_trade_id", table_name="journal_trade_tags")
    op.drop_table("journal_trade_tags")
    op.drop_table("journal_tags")
