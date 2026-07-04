"""Phase 4 broker-column + live-status checks for the ExecutionLog store.

Phase 4 adds a LIVE broker path. ``ExecutionLog`` gains three columns to track a real
broker order -- ``broker`` (e.g. "alpaca"; "" for non-broker rows), ``broker_order_id``,
``broker_status`` -- and the limit-counting status set grows to include the two live
statuses for which an order has reserved its notional: ``submitted_live`` (working, not yet
filled) and ``filled_live``. The other live statuses (``canceled`` / ``rejected_live``)
correctly do NOT count.

These tests pin the additive, behavior-preserving change:
  * a default-valued round-trip (``broker == ""``, ids ``None``) preserves the prior shape,
  * explicit broker values persist,
  * ``execution_logs_for_day`` now counts ``submitted_live`` + ``filled_live`` rows while
    still excluding ``canceled`` / ``rejected_live`` (which never reserved notional).
"""

from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import ExecutionLog
from swing_screener.db.session import get_engine


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        created_date=date(2026, 6, 21),
        ticker="AMD",
        timeframe="1d",
        play_type="continuation",
        run_date=date(2026, 6, 20),
        account="live",
        mode="alpaca",
        side="buy",
        limit_price=120.5,
        shares=10,
        stop=110.0,
        target=140.0,
        risk_dollars=105.0,
        notional=1205.0,
        status="submitted_live",
        detail="live order",
        idempotency_key="AMD|1d|continuation|2026-06-20|live",
    )
    base.update(overrides)
    return base


def test_broker_columns_default_to_empty_and_none() -> None:
    """An ExecutionLog written without broker fields keeps the prior shape: ``broker`` is
    the empty string and both broker ids are NULL (additive, behavior-preserving)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.add_execution_log(s, **_fields(status="recorded"))
        got = s.query(ExecutionLog).one()
        assert got.broker == ""
        assert got.broker_order_id is None
        assert got.broker_status is None


def test_broker_columns_persist_explicit_values() -> None:
    """Explicit broker values round-trip: the live path can stamp the broker, the broker's
    order id, and the broker-reported status onto the row."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.add_execution_log(s, **_fields(
            broker="alpaca",
            broker_order_id="abc-123",
            broker_status="new",
        ))
        got = s.query(ExecutionLog).one()
        assert got.broker == "alpaca"
        assert got.broker_order_id == "abc-123"
        assert got.broker_status == "new"


def test_execution_logs_for_day_counts_live_statuses_and_excludes_canceled() -> None:
    """``submitted_live`` (working) + ``filled_live`` reserve their notional and count;
    ``canceled`` / ``rejected_live`` never reserved it and are excluded -- alongside the
    existing ``recorded`` / ``filled_paper`` (counted) and ``skipped`` / ``rejected``
    (excluded)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # the two new live statuses both reserve notional -> count
        repo.add_execution_log(s, **_fields(
            ticker="SUBLIVE", status="submitted_live", idempotency_key="k-sublive"))
        repo.add_execution_log(s, **_fields(
            ticker="FILLLIVE", status="filled_live", idempotency_key="k-filllive"))
        # the prior counted statuses still count
        repo.add_execution_log(s, **_fields(
            ticker="REC", status="recorded", idempotency_key="k-recorded"))
        repo.add_execution_log(s, **_fields(
            ticker="FILLPAPER", status="filled_paper", idempotency_key="k-fillpaper"))
        # live statuses that never reserved notional -> excluded
        repo.add_execution_log(s, **_fields(
            ticker="CANCEL", status="canceled", idempotency_key="k-cancel"))
        repo.add_execution_log(s, **_fields(
            ticker="REJLIVE", status="rejected_live", idempotency_key="k-rejlive"))
        # the prior excluded statuses still don't count
        repo.add_execution_log(s, **_fields(
            ticker="SKIP", status="skipped", idempotency_key="k-skipped"))

        got = repo.execution_logs_for_day(
            s, run_date=date(2026, 6, 20), account="live")
        assert {r.ticker for r in got} == {"SUBLIVE", "FILLLIVE", "REC", "FILLPAPER"}


def test_latest_recorded_stop_reads_the_newest_live_ticket() -> None:
    """The disarm restore path re-arms a dead bracket stop leg at the ticket's RECORDED
    stop (copied, never computed): the newest live row wins, manual/paper rows and other
    tickers are ignored, and an unknown ticker yields None (restore refuses to guess)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.add_execution_log(s, **_fields(ticker="NVDA", stop=90.0,
                                            idempotency_key="nvda-older"))
        repo.add_execution_log(s, **_fields(ticker="NVDA", stop=95.0,
                                            status="filled_live",
                                            idempotency_key="nvda-newer"))
        repo.add_execution_log(s, **_fields(ticker="NVDA", stop=80.0, status="recorded",
                                            idempotency_key="nvda-manual"))  # not live
        repo.add_execution_log(s, **_fields(ticker="AMD", stop=50.0,
                                            idempotency_key="amd-live"))
        assert repo.latest_recorded_stop(s, "NVDA") == 95.0
        assert repo.latest_recorded_stop(s, "AMD") == 50.0
        assert repo.latest_recorded_stop(s, "TSLA") is None
