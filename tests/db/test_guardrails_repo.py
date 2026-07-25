"""Guardrails state machine + breaker queries (db.guardrails_repo).

The brake's load-bearing properties, pinned:
  * get-or-create of the SINGLE agent_guardrails row (never an explicit id --
    SQL Server IDENTITY),
  * the rows-affected TRIP election (exactly one caller owns the response;
    there is no other cross-process lock),
  * clear demands the acknowledged trip_id (a stale ack never releases the brake),
  * halt never downgrades a trip; a trip overwrites a halt,
  * edit_limits can NEVER touch the state columns,
  * every successful transition appends exactly one AgentGuardrailEvent,
  * the four breaker inputs (trades/day, realized $, drawdown-from-HWM, loss
    streak) count exactly the rows they claim to,
  * the real-money mandate refuses when any mandatory breaker is unset,
  * load_guardrails is a COLUMN select: a commit through a SECOND session is
    visible to a load through the FIRST session's identity-map-stale entity.
"""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from swing_screener.db import guardrails_repo as gr
from swing_screener.db import repo
from swing_screener.db.models import AgentGuardrailEvent, AgentGuardrails, PaperTrade
from swing_screener.db.session import get_engine


@pytest.fixture
def session():
    with Session(get_engine("sqlite:///:memory:")) as s:
        yield s


def _live_trade(
    *,
    ticker: str = "AMD",
    account: str = "live",
    status: str = "closed",
    entry_price: float | None = 100.0,
    exit_price: float | None = 110.0,
    exit_date: date | None = date(2026, 7, 17),
    qty: int | None = 10,
    realized_r: float | None = 1.0,
) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        account=account, fill_status="filled", entry_date=date(2026, 7, 10),
        entry_price=entry_price, stop=95.0, target=120.0, risk=5.0, status=status,
        exit_date=exit_date, exit_price=exit_price, realized_r=realized_r, qty=qty,
    )


def _exec_fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        created_date=date(2026, 7, 17),
        ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=date(2026, 7, 17), account="live", mode="live", side="buy",
        limit_price=120.5, shares=10, stop=110.0, target=140.0,
        risk_dollars=105.0, notional=1205.0, status="submitted_live",
        detail="order submitted", idempotency_key="k-default",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------- state machine


def test_load_creates_default_row_once(session: Session) -> None:
    first = gr.load_guardrails(session)
    assert first.state == "ok"
    assert first.max_daily_loss_usd is None
    assert first.max_trades_per_day is None
    assert first.max_drawdown_usd is None
    assert first.loss_streak_halt is None
    assert first.hwm_anchor_date is None
    assert first.hwm_baseline_usd == 0.0
    assert first.trip_id is None
    assert first.trip_reason is None
    assert first.sweep_state is None
    row_id = session.query(AgentGuardrails).one().id

    second = gr.load_guardrails(session)
    assert second == first
    # get-or-create: the second load reuses the SAME row, never inserts another.
    assert session.query(AgentGuardrails).one().id == row_id


def test_load_sees_second_session_commit_past_stale_identity_map(tmp_path) -> None:
    """The whole point of the column select: a mid-dispatch cockpit HALT committed
    through a SECOND session must be visible to a load through the FIRST session,
    even while the first session's identity map still holds the stale entity."""
    engine = get_engine(f"sqlite:///{tmp_path}/guardrails.db")
    with Session(engine) as dispatch, Session(engine) as cockpit:
        assert gr.load_guardrails(dispatch).state == "ok"
        # the long-lived dispatch Session caches the ORM entity (identity map).
        cached = dispatch.query(AgentGuardrails).one()
        assert cached.state == "ok"

        assert gr.halt(cockpit, source="cockpit") is True

        # the cached entity is STALE -- exactly what load_guardrails must bypass.
        assert cached.state == "ok"
        assert gr.load_guardrails(dispatch).state == "halted"


def test_trip_elects_exactly_one_owner(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd",
                  reason="realized -75.00 <= -50.00", source="screen")
    assert isinstance(eid, int)
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid
    assert g.trip_reason == "realized -75.00 <= -50.00"
    assert g.sweep_state == "pending"

    # a second breach while tripped: the event is still recorded (it is true the
    # breaker breached) but the election is lost -- nobody double-owns the sweep.
    second = gr.trip(session, breaker="max_drawdown_usd",
                     reason="drawdown 120.00 >= 100.00", source="digest")
    assert second is None
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid                        # first owner's id, untouched
    assert g.trip_reason == "realized -75.00 <= -50.00"
    kinds = [e.kind for e in session.query(AgentGuardrailEvent).order_by(
        AgentGuardrailEvent.id)]
    assert kinds.count("trip") == 2


def test_clear_requires_matching_trip_id(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None

    assert gr.clear(session, acknowledged_trip_id=eid + 999, source="cockpit") is False
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid
    # a failed clear changed nothing, so it appends NO event.
    assert session.query(AgentGuardrailEvent).filter_by(kind="clear").count() == 0

    assert gr.clear(session, acknowledged_trip_id=eid, source="cockpit") is True
    g = gr.load_guardrails(session)
    assert g.state == "ok"
    assert g.trip_id is None
    assert g.trip_reason is None
    assert g.sweep_state is None
    assert session.query(AgentGuardrailEvent).filter_by(kind="clear").count() == 1


def test_halt_then_trip_overwrites_halted(session: Session) -> None:
    assert gr.halt(session, source="cockpit") is True
    assert gr.load_guardrails(session).state == "halted"

    eid = gr.trip(session, breaker="loss_streak_halt", reason="streak 4 >= 3",
                  source="screen")
    assert eid is not None                          # the trip is the stronger record
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid
    assert g.sweep_state == "pending"


def test_halt_never_downgrades_tripped(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None

    assert gr.halt(session, source="cockpit") is False
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid
    # event on SUCCESS only -- the refused halt changed nothing.
    assert session.query(AgentGuardrailEvent).filter_by(kind="halt").count() == 0

    # and a second halt while already halted is refused too (WHERE state == 'ok').
    gr.clear(session, acknowledged_trip_id=eid, source="cockpit")
    assert gr.halt(session, source="cockpit") is True
    assert gr.halt(session, source="cockpit") is False
    assert session.query(AgentGuardrailEvent).filter_by(kind="halt").count() == 1


def test_clear_halt_refuses_while_tripped(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None

    # a trip releases ONLY through clear() with the acknowledged trip_id.
    assert gr.clear_halt(session, source="cockpit") is False
    g = gr.load_guardrails(session)
    assert g.state == "tripped"
    assert g.trip_id == eid
    assert g.sweep_state == "pending"
    # event on success only -- the refused release appends nothing.
    assert session.query(AgentGuardrailEvent).filter_by(kind="clear").count() == 0


def test_two_rows_transitions_still_elect_one(session: Session) -> None:
    """The seed-race survival pin: if the empty-table race ever double-seeds the
    table, every write stays pinned to the canonical MIN(id) row -- rowcount is
    capped at 1, so the election still elects exactly one owner."""
    gr.load_guardrails(session)
    session.add(AgentGuardrails(updated_at=datetime.now(UTC)))  # no explicit id
    session.commit()
    first_id, second_id = [
        r.id for r in session.query(AgentGuardrails).order_by(AgentGuardrails.id)
    ]
    assert first_id < second_id

    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None                    # rowcount 1 via the MIN-id pin
    first, second = session.query(AgentGuardrails).order_by(AgentGuardrails.id)
    assert first.state == "tripped"           # the FIRST row carries the trip
    assert first.trip_id == eid
    assert first.sweep_state == "pending"
    assert second.state == "ok"               # the stray row is never touched
    assert second.trip_id is None
    assert gr.load_guardrails(session).state == "tripped"  # reads agree on MIN(id)

    assert gr.clear(session, acknowledged_trip_id=eid, source="cockpit") is True
    first, second = session.query(AgentGuardrails).order_by(AgentGuardrails.id)
    assert first.state == "ok"
    assert first.trip_id is None
    assert second.state == "ok"
    assert gr.load_guardrails(session).state == "ok"


def test_edit_never_touches_state_columns(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None

    gr.edit_limits(session, source="cockpit", max_daily_loss_usd=50.0,
                   max_trades_per_day=3, hwm_anchor_date=date(2026, 7, 1))
    g = gr.load_guardrails(session)
    assert g.max_daily_loss_usd == 50.0
    assert g.max_trades_per_day == 3
    assert g.hwm_anchor_date == date(2026, 7, 1)
    assert g.max_drawdown_usd is None               # unpassed keys untouched
    # the brake itself is untouched: still tripped, same owner, sweep pending.
    assert g.state == "tripped"
    assert g.trip_id == eid
    assert g.sweep_state == "pending"

    # the 'edit' event carries old -> new for exactly the passed keys (dates iso).
    ev = session.query(AgentGuardrailEvent).filter_by(kind="edit").one()
    payload = json.loads(ev.values_json)
    assert payload["old"] == {"max_daily_loss_usd": None, "max_trades_per_day": None,
                              "hwm_anchor_date": None}
    assert payload["new"] == {"max_daily_loss_usd": 50.0, "max_trades_per_day": 3,
                              "hwm_anchor_date": "2026-07-01"}


def test_edit_rejects_non_limit_keys(session: Session) -> None:
    # the whitelist is the guarantee: state columns can NEVER ride through an edit.
    for bad in ({"state": "ok"}, {"trip_id": None}, {"sweep_state": "complete"},
                {"trip_reason": "x"}, {"nonsense": 1}):
        with pytest.raises(ValueError):
            gr.edit_limits(session, source="cockpit", **bad)
    with pytest.raises(ValueError):
        gr.edit_limits(session, source="cockpit")   # an empty edit is a caller bug
    assert session.query(AgentGuardrailEvent).count() == 0


def test_sweep_outcome_never_stamps_a_newer_trip(session: Session) -> None:
    """Task 6's stale-sweep guarantee: a sweep finishing late (for an already
    CLEARED trip) must not stamp the CURRENT trip's bookkeeping -- the outcome
    keys on trip_id, and the sweep event is still journaled for the Auditor."""
    old_eid = gr.trip(session, breaker="max_daily_loss_usd", reason="first breach",
                      source="screen")
    assert old_eid is not None
    assert gr.clear(session, acknowledged_trip_id=old_eid, source="cockpit") is True
    new_eid = gr.trip(session, breaker="max_drawdown_usd", reason="second breach",
                      source="digest")
    assert new_eid is not None

    gr.record_sweep_outcome(session, trip_id=old_eid, outcome="complete",
                            detail="late sweep for the cleared trip",
                            source="screen")
    g = gr.load_guardrails(session)
    assert g.trip_id == new_eid
    assert g.sweep_state == "pending"       # the NEW trip's sweep is untouched
    # ... but the sweep DID run -- its event row is journaled regardless.
    ev = session.query(AgentGuardrailEvent).filter_by(kind="sweep").one()
    assert json.loads(ev.values_json) == {"trip_id": old_eid, "outcome": "complete",
                                          "unprotected": []}


def test_sweep_outcome_journals_the_unprotected_list_structurally(
        session: Session) -> None:
    """``unprotected`` rides ``values_json`` as DATA, not only as prose inside the
    detail sentence. The cockpit's DISARM resume reads it back to raise the same
    red no-Escape alarm the raw sweep path raises from its own return value -- an
    alarm that exists only as a substring of a human sentence cannot be branched on
    without string-sniffing, and the two disarm modes would then render identical
    venue state loudly and calmly."""
    eid = gr.trip(session, breaker="max_drawdown_usd", reason="breach",
                  source="digest")
    assert eid is not None
    gr.record_sweep_outcome(session, trip_id=eid, outcome="complete",
                            detail="swept: 1 cancelled; UNPROTECTED: NVDA, AMD",
                            source="digest", unprotected=["NVDA", "AMD"])

    ev = session.query(AgentGuardrailEvent).filter_by(kind="sweep").one()
    assert json.loads(ev.values_json) == {"trip_id": eid, "outcome": "complete",
                                          "unprotected": ["NVDA", "AMD"]}
    assert gr.load_guardrails(session).sweep_state == "complete"


def test_sweep_outcome_rejects_unknown_vocabulary(session: Session) -> None:
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None
    for bad in ("done", "complete ", "PARTIAL", ""):
        with pytest.raises(ValueError):
            gr.record_sweep_outcome(session, trip_id=eid, outcome=bad,
                                    detail="x", source="screen")
    assert gr.load_guardrails(session).sweep_state == "pending"
    assert session.query(AgentGuardrailEvent).filter_by(kind="sweep").count() == 0


def test_record_event_truncates_to_column_bounds(session: Session) -> None:
    # sqlite never enforces String(N); Azure SQL raises -- and in trip() the
    # event insert runs BEFORE the state UPDATE, so an overlong value would
    # keep the brake from engaging in prod ONLY. Truncation is the guard.
    eid = gr.record_event(
        session, kind="trip", source="s" * 40, breaker="b" * 40, reason="r" * 300,
    )
    ev = session.get(AgentGuardrailEvent, eid)
    assert ev is not None
    assert ev.breaker == "b" * 32
    assert ev.source == "s" * 16
    assert ev.reason == "r" * 256


def test_edit_rejects_non_positive_breaker_values(session: Session) -> None:
    for bad in ({"max_daily_loss_usd": 0.0}, {"max_daily_loss_usd": -50.0},
                {"max_trades_per_day": 0}, {"max_drawdown_usd": -1.0},
                {"loss_streak_halt": -3}):
        with pytest.raises(ValueError):
            gr.edit_limits(session, source="cockpit", **bad)
    assert session.query(AgentGuardrailEvent).count() == 0
    # None stays allowed (= unset), and the two anchor fields are unconstrained
    # (a baseline of 0.0 is a legitimate fresh-start anchor).
    gr.edit_limits(session, source="cockpit", max_daily_loss_usd=None,
                   hwm_baseline_usd=0.0, hwm_anchor_date=date(2026, 7, 1))
    g = gr.load_guardrails(session)
    assert g.max_daily_loss_usd is None
    assert g.hwm_baseline_usd == 0.0
    assert g.hwm_anchor_date == date(2026, 7, 1)


def test_every_transition_appends_event(session: Session) -> None:
    def event_count() -> int:
        return session.query(AgentGuardrailEvent).count()

    gr.edit_limits(session, source="cockpit", max_daily_loss_usd=50.0)
    assert event_count() == 1
    assert gr.halt(session, source="cockpit") is True
    assert event_count() == 2
    assert gr.clear_halt(session, source="cockpit") is True
    assert event_count() == 3
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None
    assert event_count() == 4
    gr.record_sweep_outcome(session, trip_id=eid, outcome="complete",
                            detail="2 orders canceled, 1 stop restored",
                            source="screen")
    assert event_count() == 5
    assert gr.load_guardrails(session).sweep_state == "complete"
    assert gr.clear(session, acknowledged_trip_id=eid, source="cockpit") is True
    assert event_count() == 6

    kinds = [e.kind for e in session.query(AgentGuardrailEvent).order_by(
        AgentGuardrailEvent.id)]
    assert kinds == ["edit", "halt", "clear", "trip", "sweep", "clear"]
    # every event names its actor (journal convention: source is required).
    assert all(e.source in ("cockpit", "screen") for e in
               session.query(AgentGuardrailEvent))


# ---------------------------------------------------------------- breaker queries


def test_realized_usd_on_sums_closed_live_trades_with_qty(session: Session) -> None:
    day = date(2026, 7, 17)
    session.add_all([
        # +50: (110 - 100) * 5
        _live_trade(ticker="WIN", entry_price=100.0, exit_price=110.0, qty=5),
        # -60: (44 - 50) * 10
        _live_trade(ticker="LOSE", entry_price=50.0, exit_price=44.0, qty=10),
        # NULL qty (legacy live row): contributes 0 -- $ math skips, never guesses.
        _live_trade(ticker="LEGACY", entry_price=10.0, exit_price=90.0, qty=None),
        # other day: excluded.
        _live_trade(ticker="YDAY", exit_date=date(2026, 7, 16), qty=7),
        # other account: excluded even with qty somehow set.
        _live_trade(ticker="RSRCH", account="research", qty=9),
        # still open: no realized $ yet.
        _live_trade(ticker="OPEN", status="open", exit_date=None, exit_price=None),
    ])
    session.commit()
    assert gr.realized_usd_on(session, run_date=day) == pytest.approx(-10.0)
    # an empty day coalesces to 0.0, never NULL.
    assert gr.realized_usd_on(session, run_date=date(2026, 7, 15)) == 0.0


def test_live_realized_usd_total_sums_all_time_and_counts_unsized(session: Session) -> None:
    session.add_all([
        # +50 and -60 across two different days: all-time, no day filter.
        _live_trade(ticker="WIN", entry_price=100.0, exit_price=110.0, qty=5,
                    exit_date=date(2026, 7, 10)),
        _live_trade(ticker="LOSE", entry_price=50.0, exit_price=44.0, qty=10),
        # unpriceable closes: 0 to the sum, 1 each to the honesty count -- ONE PER
        # NULL AXIS (share count, exit price, entry price). All three axes are pinned
        # because a dropped axis under-reports n_unsized SILENTLY: the sum is safe
        # either way (SQL sums skip a NULL term), so only the counter can catch it --
        # and the counter is the whole disclosure.
        _live_trade(ticker="LEGACY", qty=None, exit_date=date(2026, 7, 10)),
        _live_trade(ticker="NOPRICE", exit_price=None),
        _live_trade(ticker="NOENTRY", entry_price=None),
        # other book / still open: neither summed nor counted.
        _live_trade(ticker="RSRCH", account="research", qty=9),
        _live_trade(ticker="OPEN", status="open", exit_date=None, exit_price=None),
    ])
    session.commit()
    assert gr.live_realized_usd_total(session) == (pytest.approx(-10.0), 3)
    # `since` cuts on the CLOSE date (the scoreboard's window axis), and the honesty
    # count rides the SAME cut as the sum -- LEGACY's 7/10 close drops from both.
    assert gr.live_realized_usd_total(session, since=date(2026, 7, 15)) == (
        pytest.approx(-60.0), 2)
    # an empty window coalesces to 0.0, never NULL.
    assert gr.live_realized_usd_total(session, since=date(2026, 8, 1)) == (0.0, 0)


def test_live_drawdown_from_anchor(session: Session) -> None:
    # three closed live trades whose ExitEvents land in this order:
    #   T1 -100, T2 +50, T3 -20  (P&L = (exit-entry)*qty)
    t1 = _live_trade(ticker="T1", entry_price=100.0, exit_price=90.0, qty=10)
    t2 = _live_trade(ticker="T2", entry_price=100.0, exit_price=105.0, qty=10)
    t3 = _live_trade(ticker="T3", entry_price=100.0, exit_price=98.0, qty=10)
    unsized = _live_trade(ticker="NOQTY", entry_price=10.0, exit_price=1.0, qty=None)
    session.add_all([t1, t2, t3, unsized])
    session.commit()
    d1, d2, d3 = date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)
    for trade, day in ((t1, d1), (t2, d2), (t3, d3)):
        repo.record_exit_event(session, is_paper=False, trade_id=trade.id,
                               tier="daily", reason="stop", message="closed",
                               created_date=day, account="live")
    # noise the walk must skip: an orphan event (NULL trade_id), an unsized
    # trade's event, and a research-book event.
    repo.record_exit_event(session, is_paper=False, trade_id=None, tier="daily",
                           reason="stop", message="orphan", created_date=d2,
                           account="live")
    repo.record_exit_event(session, is_paper=False, trade_id=unsized.id,
                           tier="daily", reason="stop", message="unsized",
                           created_date=d2, account="live")
    repo.record_exit_event(session, is_paper=True, trade_id=t1.id, tier="daily",
                           reason="stop", message="research", created_date=d3,
                           account="research")

    # full walk from baseline 0: cum -100 -> -50 -> -70; hwm stays 0 -> dd 70.
    assert gr.live_drawdown_usd(session, anchor_date=None,
                                baseline_usd=0.0) == pytest.approx(70.0)
    # anchored at d2 the -100 predates the window: cum +50 -> +30; hwm 50 -> dd 20.
    assert gr.live_drawdown_usd(session, anchor_date=d2,
                                baseline_usd=0.0) == pytest.approx(20.0)
    # the baseline seeds cum AND hwm, so the anchored dd is baseline-invariant.
    assert gr.live_drawdown_usd(session, anchor_date=d2,
                                baseline_usd=500.0) == pytest.approx(20.0)
    # no trades in the window -> 0.0, never negative.
    assert gr.live_drawdown_usd(session, anchor_date=date(2026, 8, 1),
                                baseline_usd=0.0) == 0.0


def test_live_loss_streak_ordered_by_exit_event_id(session: Session) -> None:
    assert gr.live_loss_streak(session) == 0        # empty book

    win = _live_trade(ticker="WIN", realized_r=1.2)
    l1 = _live_trade(ticker="L1", realized_r=-0.5)
    l2 = _live_trade(ticker="L2", realized_r=-0.3)
    ungraded = _live_trade(ticker="NULLR", realized_r=None)
    l3 = _live_trade(ticker="L3", realized_r=-0.8)
    rwin = _live_trade(ticker="RWIN", account="research", realized_r=2.0)
    session.add_all([win, l1, l2, ungraded, l3, rwin])
    session.commit()
    # oldest -> newest: +1.2, -0.5, orphan, -0.3, NULL-r, -0.8, research +2.0
    order: list[tuple[int | None, str]] = [
        (win.id, "live"), (l1.id, "live"), (None, "live"), (l2.id, "live"),
        (ungraded.id, "live"), (l3.id, "live"), (rwin.id, "research"),
    ]
    for trade_id, account in order:
        repo.record_exit_event(session, is_paper=account != "live",
                               trade_id=trade_id, tier="daily", reason="stop",
                               message="", created_date=date(2026, 7, 17),
                               account=account)
    # newest first: research row invisible; -0.8 counts; NULL-r SKIPPED (scan
    # continues); -0.3 counts; orphan skipped; -0.5 counts; +1.2 stops the scan.
    assert gr.live_loss_streak(session) == 3


def test_trades_today_counts_counting_statuses_only(session: Session) -> None:
    day = date(2026, 7, 17)
    counting = [("SUB", "submitted_live"), ("FILL", "filled_live")]
    non_counting = [("SKIP", "skipped"), ("REJ", "rejected_live"),
                    ("CAN", "canceled")]
    for ticker, status in counting + non_counting:
        repo.add_execution_log(session, **_exec_fields(
            ticker=ticker, status=status, idempotency_key=f"k-{ticker}"))
    # other account and other day never count against the live cap.
    repo.add_execution_log(session, **_exec_fields(
        ticker="PAPER", account="paper", mode="paper", status="filled_paper",
        idempotency_key="k-paper"))
    repo.add_execution_log(session, **_exec_fields(
        ticker="YDAY", run_date=date(2026, 7, 16), idempotency_key="k-yday"))

    assert gr.trades_today(session, run_date=day) == 2


def test_peek_never_seeds_and_answers_the_load_default(session: Session) -> None:
    """``peek_guardrails`` is ``load_guardrails`` minus the get-or-create.

    On an empty table it answers ``_UNSEEDED`` and writes NOTHING (the read-only
    surfaces -- preflight, the cockpit polls -- must not INSERT under a read-only DB
    grant). The constant must be BYTE-IDENTICAL to the row load would have seeded, or
    the two readers would disagree about a brake nobody has configured yet: that is
    pinned here against the real seed, so a model default change breaks this test
    rather than the safety screen."""
    assert gr.peek_guardrails(session) == gr._UNSEEDED
    assert session.query(AgentGuardrails).count() == 0   # peek seeded nothing

    seeded = gr.load_guardrails(session)                 # NOW the row is created
    assert session.query(AgentGuardrails).count() == 1
    assert seeded == gr._UNSEEDED                        # byte-identical to the peek
    assert gr.peek_guardrails(session) == seeded         # and agrees once it exists


def test_peek_sees_another_sessions_committed_transition(session: Session) -> None:
    """Same column-select freshness as load: a HALT committed elsewhere is visible
    immediately (the masthead chip polls through this)."""
    gr.load_guardrails(session)
    with Session(session.get_bind()) as other:
        assert gr.halt(other, source="cockpit") is True
    assert gr.peek_guardrails(session).state == "halted"


def test_mandate_from_state_is_pure_and_shares_one_definition(session: Session) -> None:
    """The mandate over a snapshot you already hold -- no session, no write -- and the
    SAME verdict the seeding enforcement entry gives."""
    assert gr.mandate_from_state(gr._UNSEEDED) == (False, "max_daily_loss_usd is not set")
    gr.edit_limits(session, source="cockpit", max_daily_loss_usd=50.0,
                   max_trades_per_day=3, max_drawdown_usd=200.0)
    g = gr.peek_guardrails(session)
    assert gr.mandate_from_state(g) == (True, "")
    assert gr.guardrails_mandate_ok(session) == gr.mandate_from_state(g)


def test_mandate_ok_requires_three_breakers_set(session: Session) -> None:
    # unset breakers refuse, naming the FIRST missing one in mandate order.
    assert gr.guardrails_mandate_ok(session) == (False, "max_daily_loss_usd is not set")
    gr.edit_limits(session, source="cockpit", max_daily_loss_usd=50.0)
    assert gr.guardrails_mandate_ok(session) == (False, "max_trades_per_day is not set")
    gr.edit_limits(session, source="cockpit", max_trades_per_day=3)
    assert gr.guardrails_mandate_ok(session) == (False, "max_drawdown_usd is not set")
    gr.edit_limits(session, source="cockpit", max_drawdown_usd=200.0)
    assert gr.guardrails_mandate_ok(session) == (True, "")
    # loss_streak_halt stays optional -- never part of the mandate.

    assert gr.halt(session, source="cockpit") is True
    assert gr.guardrails_mandate_ok(session) == (False, "guardrails state is halted")
    assert gr.clear_halt(session, source="cockpit") is True
    eid = gr.trip(session, breaker="max_daily_loss_usd", reason="breach",
                  source="screen")
    assert eid is not None
    assert gr.guardrails_mandate_ok(session) == (False, "guardrails state is tripped")
