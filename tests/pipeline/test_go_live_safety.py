"""Go-live safety verification -- the TEETH behind the Alpaca-live arming runbook.

The runbook (``docs/runbooks/arming-alpaca-live.md``) promises three load-bearing
guards; this module pins the ones not already explicitly covered elsewhere, so the
prose can never drift from the shipped code:

* ``is_real_money()`` correctly flags the REAL Alpaca live host
  (``https://api.alpaca.markets``) as real money and the paper host as fake -- the
  property preflight surfaces and the LiveAdapter's real-money guard hinges on.
* the ``allow_real_money`` lock, on its OWN -- a real-money endpoint that is
  ``execution_mode=live`` AND has a ready gate AND every cap set, but is MISSING the
  explicit ``SWING_BROKER_ALLOW_REAL_MONEY`` flag, is still REFUSED (no broker order).
  (The mode lock and the gate lock are pinned in ``test_execution_live.py``; this
  closes the third lock so all three locks are independently covered.)
* a real-money endpoint with a cap left UNSET is refused even when only ONE cap is
  missing (a partially-capped config, not just the all-unbounded case the existing
  ``test_real_money_with_unset_cap_is_rejected`` covers) -- real money never runs
  with any unbounded cap.

This does NOT duplicate existing coverage -- it cites and complements it:

* ``test_broker_alpaca.py::test_is_real_money_true_for_live_host`` /
  ``::test_is_real_money_false_for_paper_host`` /
  ``::test_is_real_money_true_for_unknown_host_fail_safe`` already pin the host->money
  mapping at the broker level; the live-host case here re-asserts it through the
  module's ``PAPER_HOST`` constant to anchor the runbook's exact host strings.
* ``test_execution_live.py::test_real_money_without_gate_is_rejected_no_broker_call``
  (gate lock) and ``::test_real_money_mode_not_live_is_rejected`` (mode lock) pin two
  of the three locks; ``::test_real_money_with_unset_cap_is_rejected`` pins the
  all-unbounded caps refusal; ``::test_real_money_with_all_locks_and_caps_submits``
  pins the happy path.

No live network anywhere (in-memory SQLite + the venue-free ``FakeBroker``).
"""

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.db.models import ExecutionLog, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.broker import FakeBroker
from swing_screener.pipeline.broker_alpaca import PAPER_HOST, AlpacaBroker
from swing_screener.pipeline.execution import LiveAdapter
from swing_screener.pipeline.insight import OrderIntent
from swing_screener.settings import Limits, Settings

#: The REAL Alpaca live host the runbook tells the operator to set as ``SWING_ALPACA_HOST``.
LIVE_HOST = "https://api.alpaca.markets"

RUN = date(2026, 6, 19)
#: every cap set -- the floor a real-money endpoint must clear (``real_money_limits_ok``).
FULL_CAPS = Limits(max_daily_notional=100_000.0, max_daily_loss=2.0, max_concurrent=5)


def _intent(**over: object) -> OrderIntent:
    base: dict[str, object] = dict(
        ticker="AMD", timeframe="1d", play_type="continuation",
        entry_floor=99.0, entry_ceiling=101.0, stop=94.0, target=110.0,
        conviction="high", shares=10, risk_dollars=70.0,
        edge_played="e", key_risk="", insight="i",
        side="long", limit_price=101.0,
    )
    base.update(over)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _live_settings(*, mode: str = "live", allow: bool = True) -> Settings:
    """A Settings snapshot with only the two locks ``can_arm_real_money`` reads set as
    asked; the rest is constructible filler. Built directly (not via env) so the test
    controls the locks without monkeypatching the environment."""
    return Settings(
        db_url="sqlite:///:memory:", chart_dir=Path("."), cache_dir=Path("."),
        pdf_dir=Path("."), edge_dir=Path("."), blob_account_url=None, blob_container="c",
        key_vault_url=None,
        azure_client_id=None, acs_endpoint=None, acs_sender=None,
        deep_analysis_enabled=False, analysis_model="m", analysis_reasoning="high",
        deep_analysis_top_n=5, deep_analysis_kinds=frozenset(), analysis_max_searches=4,
        deep_analysis_max_usd=None,
        account_equity=None, risk_per_trade_dollars=None, risk_pct=0.01, max_shares=None,
        execution_mode=mode, max_daily_notional=None, max_daily_loss=None,
        max_concurrent=None, broker="alpaca", allow_real_money=allow,
    )


# ---------------------------------------------------------------------------
# is_real_money flags the REAL Alpaca live host as real money, the paper host as fake.
# (Re-asserted through the module's PAPER_HOST constant so the runbook's exact host
#  strings stay anchored to the code; the broker-level matrix lives in
#  test_broker_alpaca.py::test_is_real_money_{true_for_live_host,false_for_paper_host}.)
# ---------------------------------------------------------------------------
def test_alpaca_live_host_is_real_money_paper_host_is_not() -> None:
    live = AlpacaBroker(key="k", secret="s", host=LIVE_HOST)
    paper = AlpacaBroker(key="k", secret="s", host=PAPER_HOST)
    try:
        assert live.is_real_money() is True       # live host -> REAL money
        assert paper.is_real_money() is False      # paper sandbox -> fake money
    finally:
        live._client.close()
        paper._client.close()


# ---------------------------------------------------------------------------
# the THIRD lock, on its own: allow_real_money MISSING -> refused, no broker order.
# (mode + gate locks are pinned in test_execution_live.py; this closes the set.)
# ---------------------------------------------------------------------------
def test_real_money_without_allow_flag_is_rejected_no_broker_call() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        # live mode + a READY gate + every cap, but allow_real_money is FALSE -> refused
        # on the allow lock alone (no single misconfig can move real money).
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=False),
                              gate_ready_fn=lambda _s: True)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=FULL_CAPS)

        assert result.status == "rejected"
        assert result.account == "live"
        assert "allow_real_money" in result.detail.lower()
        assert broker.list_open_orders() == []            # the broker was never touched
        assert s.query(PaperTrade).count() == 0
        rejected = s.query(ExecutionLog).filter_by(status="rejected_live").one()
        assert rejected.account == "live"


# ---------------------------------------------------------------------------
# a PARTIALLY-capped real-money config is refused too: every lock + a notional cap,
# but max_daily_loss left unset -> refused on the first missing cap, no broker order.
# (test_execution_live.py::test_real_money_with_unset_cap_is_rejected covers the
#  all-unbounded case; this pins that ONE missing cap is enough to refuse.)
# ---------------------------------------------------------------------------
def test_real_money_with_partial_caps_is_rejected_no_broker_call() -> None:
    with _session() as s:
        broker = FakeBroker(real_money=True)
        adapter = LiveAdapter(broker, settings=_live_settings(mode="live", allow=True),
                              gate_ready_fn=lambda _s: True)
        # notional + concurrent set, but max_daily_loss is None -> the caps mandate refuses.
        partial = Limits(max_daily_notional=100_000.0, max_daily_loss=None, max_concurrent=5)
        result = adapter.submit(_intent(), session=s, run_date=RUN, limits=partial)

        assert result.status == "rejected"
        assert "max_daily_loss" in result.detail.lower()
        assert broker.list_open_orders() == []            # the venue is never touched
        assert s.query(PaperTrade).count() == 0
        assert s.query(ExecutionLog).filter_by(status="rejected_live").count() == 1
