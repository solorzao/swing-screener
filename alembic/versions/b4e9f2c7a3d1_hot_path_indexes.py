"""hot-path indexes + filtered-unique import_key (Azure SQL stops full-scanning)

Revision ID: b4e9f2c7a3d1
Revises: e7c4a9f1b3d8
Create Date: 2026-07-18

Three unindexed hot-path predicates full-scanned forever-growing tables on Azure
SQL serverless (2026-07-17 audit, I1), plus one latent mssql uniqueness trap:

* ``ix_signals_run_date`` -- run_date is the predicate of every hot signals path:
  latest_run_date (ORDER BY run_date DESC LIMIT 1, per cockpit picks poll),
  latest_signals, delete_signals_for, prior_first_seen. The table grows daily.
* ``ix_paper_trades_status_account`` -- the status-driven loaders on the LARGEST
  table (the shadow grid multiplies rows per arm x variant daily): open/pending/
  closed books, count_open_positions (the pre-trade position-cap gate),
  realized_r_on (the per-day-loss breaker). Status is always an equality
  predicate and leads; account follows as equality / ``!= 'live'`` / absent.
* ``ix_exit_events_created_date`` -- exit_events_for filters on it hourly (the
  intraday exit checker's dedup); the cockpit reference screen sorts by it.
* ``uq_option_paper_trades_import_key`` -- recreated FILTERED (WHERE import_key
  IS NOT NULL). The column is NULLABLE (every non-imported paper trade skips
  it), but f2a9c4e7b1d8 created the unique index PLAIN, and on SQL Server a
  plain unique index admits only ONE NULL row -- the second key-less paper
  trade would be rejected. sqlite gets the same WHERE via sqlite_where (it
  allowed multiple NULLs either way; the filter keeps both dialects honest).

Index names match the models in db/models.py exactly. A create_all-born
local.db already carries the model-side indexes but no alembic_version --
``alembic stamp head`` it first (existing convention: local sqlite never
self-migrates); this migration is for Azure SQL, where Alembic owns the schema.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "b4e9f2c7a3d1"
down_revision: Union[str, Sequence[str], None] = "e7c4a9f1b3d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_signals_run_date", "signals", ["run_date"])
    op.create_index("ix_paper_trades_status_account", "paper_trades",
                    ["status", "account"])
    op.create_index("ix_exit_events_created_date", "exit_events", ["created_date"])

    # drop the plain unique index (single-NULL trap on mssql), recreate filtered.
    op.drop_index("uq_option_paper_trades_import_key",
                  table_name="option_paper_trades")
    op.create_index(
        "uq_option_paper_trades_import_key",
        "option_paper_trades",
        ["import_key"],
        unique=True,
        mssql_where=sa.text("import_key IS NOT NULL"),
        sqlite_where=sa.text("import_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_option_paper_trades_import_key",
                  table_name="option_paper_trades")
    op.create_index("uq_option_paper_trades_import_key", "option_paper_trades",
                    ["import_key"], unique=True)

    op.drop_index("ix_exit_events_created_date", table_name="exit_events")
    op.drop_index("ix_paper_trades_status_account", table_name="paper_trades")
    op.drop_index("ix_signals_run_date", table_name="signals")
