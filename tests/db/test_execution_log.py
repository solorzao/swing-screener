"""Round-trip + idempotency + limit-source checks for the ExecutionLog store.

Phase 3's execution adapters share one append-only ``ExecutionLog`` table that does
quadruple duty: the idempotency guard (a unique key per intent x run so a force-resent
or hourly digest never double-submits), the audit trail, the source for the hard-limit
sums (per-day notional/loss), and what the ``manual`` adapter records (the order ticket).

These tests pin the load-bearing behavior:
  * a full-field round-trip,
  * the unique-key idempotent NO-OP (a duplicate ``add_execution_log`` for the same
    ``idempotency_key`` rolls back and returns the FIRST row -- count stays 1),
  * ``execution_logs_for_day`` filters by run_date + account and EXCLUDES ``skipped``/
    ``rejected`` (only ``recorded`` / ``filled_paper`` count against the limits),
  * ``count_open_positions`` counts only OPEN paper trades for that account.
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.session import get_engine


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        created_date=date(2026, 6, 21),
        ticker="AMD",
        timeframe="1d",
        play_type="continuation",
        run_date=date(2026, 6, 20),
        account="paper",
        mode="manual",
        side="buy",
        limit_price=120.5,
        shares=10,
        stop=110.0,
        target=140.0,
        risk_dollars=105.0,
        notional=1205.0,
        status="recorded",
        detail="manual order ticket",
        idempotency_key="AMD|1d|continuation|2026-06-20|paper",
    )
    base.update(overrides)
    return base


def test_execution_log_roundtrip_all_fields() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        row = repo.add_execution_log(s, **_fields())
        assert row.id is not None
        got = s.query(ExecutionLog).one()
        assert got.created_date == date(2026, 6, 21)
        assert got.ticker == "AMD"
        assert got.timeframe == "1d"
        assert got.play_type == "continuation"
        assert got.run_date == date(2026, 6, 20)
        assert got.account == "paper"
        assert got.mode == "manual"
        assert got.side == "buy"
        assert got.limit_price == 120.5
        assert got.shares == 10
        assert got.stop == 110.0
        assert got.target == 140.0
        assert got.risk_dollars == 105.0
        assert got.notional == 1205.0
        assert got.status == "recorded"
        assert got.detail == "manual order ticket"
        assert got.idempotency_key == "AMD|1d|continuation|2026-06-20|paper"


def test_duplicate_idempotency_key_is_a_noop_returning_first_row() -> None:
    """A second add with the SAME idempotency_key rolls back and returns the FIRST row.

    The force-resent / hourly-digest guard: the table stays at one row and the returned
    row is the original (its id/detail), never a second insert.
    """
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        first = repo.add_execution_log(s, **_fields(detail="first ticket"))
        again = repo.add_execution_log(s, **_fields(detail="SECOND attempt"))
        # idempotent: same row back, table still has exactly one row.
        assert again.id == first.id
        assert again.detail == "first ticket"  # the original, not the re-attempt
        assert s.query(ExecutionLog).count() == 1


def test_add_execution_log_upgrades_skipped_to_submitted() -> None:
    """A ``skipped`` row superseded by a COUNTING write on the same key upgrades in place.

    The untracked-real-money bug: a limit-clamped ``skipped`` row used to swallow a later
    successful submit's ``submitted_live`` write on the same key -- the broker order was
    live but the reconciler (which scans ``submitted_live`` only) never saw it. The
    upgrade must also carry the per-outcome fields: ``broker_order_id`` above all (the
    reconciler skips a submitted_live row without one), and the order spec (the key
    hashes the intent identity, not its levels -- a later re-run can carry fresh levels).
    """
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        first = repo.add_execution_log(s, **_fields(
            status="skipped", detail="per-day notional cap"))
        upgraded = repo.add_execution_log(s, **_fields(
            status="submitted_live", detail="order submitted",
            broker="fake", broker_order_id="fake-0", broker_status="new",
            limit_price=121.0, notional=1210.0, stop=111.0,
        ))
        assert upgraded.id == first.id                    # same row, not a second insert
        assert s.query(ExecutionLog).count() == 1
        got = s.query(ExecutionLog).one()
        assert got.status == "submitted_live"
        assert got.detail == "order submitted"
        assert got.broker_order_id == "fake-0" and got.broker_status == "new"
        assert got.limit_price == 121.0 and got.notional == 1210.0 and got.stop == 111.0


def test_add_execution_log_never_downgrades_counting_status() -> None:
    """A COUNTING row is never downgraded by a later non-counting write on the same key."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        first = repo.add_execution_log(s, **_fields(
            status="filled_paper", detail="paper position opened"))
        again = repo.add_execution_log(s, **_fields(
            status="skipped", detail="per-day notional cap"))
        assert again.id == first.id
        got = s.query(ExecutionLog).one()
        assert got.status == "filled_paper"               # never downgraded
        assert got.detail == "paper position opened"      # nothing refreshed either
        assert s.query(ExecutionLog).count() == 1


def test_execution_logs_for_day_filters_by_date_account_and_excludes_skipped() -> None:
    """Only ``recorded`` / ``filled_paper`` rows for that run_date + account count.

    ``skipped`` / ``rejected`` are excluded (they never submitted, so they must not
    inflate the per-day notional/loss limit sums), as are other days and accounts.
    """
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.add_execution_log(s, **_fields(
            ticker="REC", status="recorded",
            idempotency_key="k-recorded"))
        repo.add_execution_log(s, **_fields(
            ticker="FILL", status="filled_paper",
            idempotency_key="k-filled"))
        # excluded by status (didn't submit -> doesn't count against limits)
        repo.add_execution_log(s, **_fields(
            ticker="SKIP", status="skipped", idempotency_key="k-skipped"))
        repo.add_execution_log(s, **_fields(
            ticker="REJ", status="rejected", idempotency_key="k-rejected"))
        # excluded by a different run_date
        repo.add_execution_log(s, **_fields(
            ticker="OTHERDAY", run_date=date(2026, 6, 19),
            idempotency_key="k-otherday"))
        # excluded by a different account
        repo.add_execution_log(s, **_fields(
            ticker="OTHERACCT", account="research", idempotency_key="k-otheracct"))

        got = repo.execution_logs_for_day(
            s, run_date=date(2026, 6, 20), account="paper")
        assert {r.ticker for r in got} == {"REC", "FILL"}


def test_count_open_positions_counts_only_open_trades_for_that_account() -> None:
    """The per-account open-position count: open trades for that account only."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        def _pt(*, ticker: str, account: str, status: str) -> PaperTrade:
            return PaperTrade(
                ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8,
                rank=1, account=account, fill_status="filled", stop=95.0, target=110.0,
                risk=5.0, status=status,
            )
        s.add_all([
            _pt(ticker="A", account="paper", status="open"),
            _pt(ticker="B", account="paper", status="open"),
            _pt(ticker="C", account="paper", status="closed"),   # not open
            _pt(ticker="D", account="research", status="open"),  # other account
        ])
        s.commit()
        assert repo.count_open_positions(s, account="paper") == 2
        assert repo.count_open_positions(s, account="research") == 1
