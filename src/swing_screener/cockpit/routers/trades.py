"""The real-trades surface: log a trade, close a trade, the positions screen, and
the log-trade prefill. Moved verbatim out of ``cockpit/api.py``; validation models,
override stamping, and every wire shape are unchanged."""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.pl import PositionPL, position_pl
from swing_screener.cockpit.common import (
    ActionNonce,
    _armed_symbols,
    _bracket,
    _require_cockpit,
)
from swing_screener.cockpit.livedata import BrokerSnapshot, QuoteCache, Snapshot
from swing_screener.db.models import (
    ExecutionLog,
    JournalReview,
    PaperTrade,
    Signal,
    Trade,
)
from swing_screener.db.repo import (
    ENTRY_SIDES,
    AlreadyClosedError,
    add_trade,
    close_trade_with_event,
    count_open_positions,
    create_coach_draft_request,
    execution_logs_for_day,
    get_closed_trades,
    get_open_trades,
    latest_run_date,
    load_open_live_trades,
    realized_r_on,
)
from swing_screener.journal.auto_tag import propose_tags
from swing_screener.journal.coach_grade import equity_review_facts, facts_dict
from swing_screener.pipeline.execution import (
    LIVE_ACCOUNT,
    MANUAL_ACCOUNT,
    OFF_ACCOUNT,
    PAPER_ACCOUNT,
)
from swing_screener.pipeline.insight import size_order
from swing_screener.settings import (
    load_settings,
    resolve_execution,
    resolve_risk_unit,
)
from swing_screener.signals.actionability import classify


class TradeCreate(BaseModel):
    """POST /api/trades body. The validation LIVES HERE -- ``repo.add_trade`` is a
    pure persist that checks nothing -- so every rule the retired Streamlit entry
    form enforced is a model rule: ticker required (stripped + uppercased), entry
    and size positive, stop below entry, target above entry (long-only book).
    ``max_length`` bounds mirror the Trade columns so Azure SQL never truncates.
    Template rule for every action model: floats pin ``allow_inf_nan=False`` --
    hand-crafted JSON ``Infinity``/``NaN`` sails through ``gt`` and comparison
    validators (NaN compares False against everything) and would poison R math."""

    ticker: str = Field(max_length=16)
    timeframe: str = Field(default="1d", max_length=32)
    horizon: str = Field(default="medium", max_length=32)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    size: float = Field(gt=0, allow_inf_nan=False)
    stop: float = Field(allow_inf_nan=False)
    target: float = Field(allow_inf_nan=False)
    notes: str = Field(default="", max_length=256)
    signal_id: int | None = None
    # Journal v2: the discretionary entry emotion (FOMO, calm...). Manual actions only.
    emotional_state: str | None = Field(default=None, max_length=32)

    @field_validator("ticker")
    @classmethod
    def _ticker_required_upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker is required")
        return v

    @model_validator(mode="after")
    def _long_geometry(self) -> "TradeCreate":
        if self.stop >= self.entry_price:
            raise ValueError("stop must be below the entry price (long)")
        if self.target <= self.entry_price:
            raise ValueError("target must be above the entry price (long)")
        return self


class TradeClose(BaseModel):
    """POST /api/trades/{id}/close body. ``exit_date`` defaults to today at the
    endpoint (a request body should not bake in the server's clock) and is
    range-checked there too -- the bounds need the trade row. A blank reason
    falls back to ``manual`` -- the Streamlit close form's behavior."""

    exit_price: float = Field(gt=0, allow_inf_nan=False)
    exit_date: date | None = None
    exit_reason: str = Field(default="manual", max_length=32)

    @field_validator("exit_reason")
    @classmethod
    def _reason_or_manual(cls, v: str) -> str:
        return v.strip() or "manual"


# Prices within this absolute tolerance count as EQUAL for override stamping: the
# prefill round-trips through JSON floats and a UI number input, so exact equality
# would stamp phantom "moved 0.0%" overrides on faithful fills.
# Mirrored client-side by FAITHFUL_TOL + overridePreview in
# cockpit-ui/src/components/LogTradeForm.tsx (the live preview) -- keep in sync.
_FAITHFUL_TOL = 0.005

log = logging.getLogger(__name__)


def _write_close_review(session: Session, trade: Trade) -> None:
    """On a manual close: stamp the deterministic Coach review facts row (with parked
    auto-tag proposals) and enqueue the async LLM narrative draft. NEVER raises -- the
    close already committed, so a review failure must not 500 the close. The LLM prose
    stays server-side (the worker drains the queue); this path calls no model.
    """
    try:
        facts = equity_review_facts(trade)
        payload = facts_dict(facts)
        payload["tag_proposals"] = [asdict(p) for p in propose_tags(facts)]
        review = JournalReview(
            identity_key=f"trade_close:manual_equity:{trade.id}",
            kind="trade_close",
            book="manual_equity",
            trade_id=trade.id,
            facts_json=json.dumps(payload),
            source="analyst",
            generated_at=datetime.now(UTC),
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        create_coach_draft_request(
            session, review_id=review.id, requested_at=datetime.now(UTC)
        )
    except Exception:
        log.warning("coach close-review write failed for trade %s", trade.id, exc_info=True)
        session.rollback()


def _pct_part(label: str, actual: float, planned: float) -> str | None:
    """One ``'{label} moved {+x.x%}'`` part, or None when the move isn't real:
    within the absolute tolerance, an unusable denominator, or -- the high-price
    trap -- a move whose FORMATTED percent rounds to ±0.0% (a one-cent nudge of a
    $500 stop clears the absolute tolerance but stamps a zero-looking override)."""
    if planned <= 0 or abs(actual - planned) <= _FAITHFUL_TOL:
        return None
    text = f"{(actual - planned) / planned:+.1%}"
    if text in ("+0.0%", "-0.0%"):
        return None
    return f"{label} moved {text}"


def _override_note(body: TradeCreate, sig: Signal) -> str | None:
    """The honest-flagging stamp: how ``body`` deviates from ``sig``'s plan, or None.

    Entry deviation is expressed in zone-R (risk = entry_ceiling - stop, the unit the
    signal's own R math uses); stop/target moves in percent of the signal's level,
    with zero-LOOKING moves suppressed (see ``_pct_part``). Format (rendered
    VERBATIM by the UI -- see the endpoint docstring):
    ``entry +0.50R above ceiling; stop moved +1.1%; target moved -2.0%``. A
    degenerate zone (ceiling <= stop, no R unit to speak in) skips the entry part
    rather than dividing by zero.

    Mirrored client-side by ``overridePreview`` in
    ``cockpit-ui/src/components/LogTradeForm.tsx`` (the log-trade form's live
    preview) -- same format, tolerances, and zone-R unit. Keep the two in sync:
    the server's stamp here is the record, the client copy only previews it.
    """
    parts: list[str] = []
    risk = sig.entry_ceiling - sig.stop
    if risk > _FAITHFUL_TOL:
        if body.entry_price > sig.entry_ceiling + _FAITHFUL_TOL:
            over = (body.entry_price - sig.entry_ceiling) / risk
            parts.append(f"entry +{over:.2f}R above ceiling")
        elif body.entry_price < sig.entry_floor - _FAITHFUL_TOL:
            under = (sig.entry_floor - body.entry_price) / risk
            parts.append(f"entry -{under:.2f}R below floor")
    for part in (_pct_part("stop", body.stop, sig.stop),
                 _pct_part("target", body.target, sig.target)):
        if part is not None:
            parts.append(part)
    return "; ".join(parts)[:256] if parts else None  # cap: the column is String(256)


def build_trades_router(
    *,
    _session: Callable[[], Iterator[Session]],
    quote_cache: QuoteCache,
    broker_snapshot: BrokerSnapshot,
    action_nonce: ActionNonce,
) -> APIRouter:
    """The trades endpoints, closed over the app's seams: the session dependency,
    the two livedata caches (the same instances parked on ``app.state``), and the
    post-action wake nonce (bumped by the two write actions here)."""
    router = APIRouter()

    @router.post("/api/trades", dependencies=[Depends(_require_cockpit)])
    def log_trade(
        body: TradeCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Log a REAL trade Oliver actually took -- records it, places no order.

        Contract: header-guarded (``_require_cockpit``); ``TradeCreate`` is the
        only validation gate (422 with field detail -- the repo persists blindly);
        the server stamps ``entry_date`` = today, never the client. When the body
        carries a ``signal_id``, the Signal row MUST exist -- 422 ``unknown
        signal_id`` otherwise, on every backend identically: the FK is real, so
        sqlite (no FK pragma) would store a dangling id silently while Azure SQL
        would reject the insert with an IntegrityError-shaped 503. With the row,
        the fill is verified against the engine's plan and any deviation is
        stamped into ``override`` in the format the UI renders verbatim:
        ``entry +0.50R above ceiling`` / ``entry -0.25R below floor`` (zone-R:
        risk = entry_ceiling - stop), ``stop moved +1.1%``, ``target moved -2.0%``,
        parts joined by ``'; '``, capped at 256 chars, zero-looking percents
        suppressed. ``override`` is null when the fill is faithful OR unprefilled
        -- the UI's unlinked tag (``signal_id`` null) tells those apart.
        """
        override: str | None = None
        if body.signal_id is not None:
            sig = session.get(Signal, body.signal_id)
            if sig is None:
                raise HTTPException(status_code=422, detail="unknown signal_id")
            override = _override_note(body, sig)
        trade = add_trade(session, Trade(
            ticker=body.ticker, timeframe=body.timeframe, horizon=body.horizon,
            entry_date=datetime.now(UTC).date(), entry_price=body.entry_price, size=body.size,
            stop=body.stop, target=body.target, notes=body.notes,
            signal_id=body.signal_id, override=override,
            emotional_state=body.emotional_state,
        ))
        action_nonce.bump()  # post-action wake: add_trade has committed
        return {"trade_id": trade.id, "override": trade.override,
                "entry_date": trade.entry_date.isoformat()}

    @router.post("/api/trades/{trade_id}/close", dependencies=[Depends(_require_cockpit)])
    def close_trade_action(
        trade_id: int, body: TradeClose, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Close a real trade at the price Oliver reports.

        Header-guarded (``_require_cockpit``); 404 on an unknown id, 409 when
        already closed (the repo's ``AlreadyClosedError`` -- re-closing would
        overwrite the recorded exit), 422 when ``exit_date`` predates the trade's
        entry or postdates today (input validation, checked post-fetch because it
        needs the trade row). The close and its ``ExitEvent(reason='manual_close')``
        -- audit trail + change token -- land in ONE transaction
        (``close_trade_with_event``): both rows or neither, so a mid-close failure
        leaves the trade open and a retry succeeds instead of 409ing against a
        half-recorded close. The manual_close reason is EXCLUDED from
        ``pending_exit_alerts``, so the hourly exit job never emails an urgent
        alert about a close performed seconds ago in the cockpit. ``realized_r``
        is ``(exit - entry) / (entry - stop)`` and null when the recorded risk is
        degenerate (stop raised to/above entry, e.g. breakeven management) -- the
        close itself still happens; ``realized_usd`` is ``(exit - entry) * size``
        and always computes.
        """
        pre = session.get(Trade, trade_id)  # for exit_date bounds + the event message
        if pre is None:
            raise HTTPException(status_code=404, detail=f"no trade with id {trade_id}")
        exit_date = body.exit_date if body.exit_date is not None else datetime.now(UTC).date()
        if exit_date < pre.entry_date:
            raise HTTPException(
                status_code=422, detail="exit_date is before the trade's entry_date")
        if exit_date > datetime.now(UTC).date():
            raise HTTPException(status_code=422, detail="exit_date is in the future")
        try:
            trade, _event = close_trade_with_event(
                session, trade_id, exit_date=exit_date, exit_price=body.exit_price,
                exit_reason=body.exit_reason, event_reason="manual_close",
                event_message=f"{pre.ticker} closed manually @ {body.exit_price:g}",
                created_date=datetime.now(UTC).date(),
            )
        except AlreadyClosedError as exc:  # BEFORE ValueError: it subclasses it
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:  # unknown id -- unreachable after the fetch, kept
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _write_close_review(session, trade)  # sync facts + enqueue async draft (best-effort)
        action_nonce.bump()  # post-action wake: close + ExitEvent committed together
        risk = trade.entry_price - trade.stop
        realized_r = (body.exit_price - trade.entry_price) / risk if risk > 0 else None
        return {
            "trade_id": trade.id,
            "realized_r": realized_r,
            "realized_usd": (body.exit_price - trade.entry_price) * trade.size,
            "exit_date": exit_date.isoformat(),
            "exit_reason": trade.exit_reason,
        }

    @router.get("/api/positions")
    def positions(session: Session = Depends(_session)) -> dict[str, object]:
        """The whole trades screen in one read: open positions with per-row P/L,
        the three cap gauges, closed history, and the realized equity curve.

        Nuances, each deliberate:

        * PER-ROW degradation: ONE malformed trade or missing quote must never
          503 the zone. Every open row is KEPT; a row whose ``position_pl``
          raises (non-positive risk, or a degenerate zero price) degrades to
          ``pl: null`` with the badge STILL computed from price/stop/target (a
          breached stop must read red even on a malformed row); a missing quote
          (absent from the cache's dict) nulls ``last_close`` and the badge is
          ``unknown``. ``last_close`` is the close form's prefill source, so it
          stays populated whenever the quote exists -- even on a row whose P/L
          math is broken.
        * ``pl.unrealized_pct`` is a FRACTION on the wire (0.04, not 4.0) --
          the frontend formats percents, mirroring ``analytics.pl``.
        * ``badge``: ``red`` price <= stop, ``yellow`` price >= target,
          ``green`` between, ``unknown`` without a price.
        * LIVE rows (open ``account="live"`` PaperTrades) size off VENUE TRUTH:
          ``size``/dollar P/L use ``PaperTrade.qty`` -- the broker's ``filled_qty``,
          stamped by the reconciler -- so a PARTIAL fill renders the shares actually
          held, never the ticket's requested shares. Only a legacy NULL-``qty`` row
          falls back to the spec'd ExecutionLog join (the NEWEST row for the ticker
          with status ``submitted_live``/``filled_live`` -- ``_live_shares``); with
          neither, both stay null with the R-multiple still rendered from the
          persisted per-share ``risk``; the size-independent percent fields stay
          honest either way. A pending-entry live row
          (``entry_price`` null) carries ``pl: null``; its badge still reads off
          the quote (stop/target are always recorded).
        * BRACKET lamp, per kind: no broker snapshot -> ``unknown`` (absence of
          evidence is never a claim); a venue-held protective sell order
          (``order_type`` in disarm's ``_STOP_TYPES``) for the symbol ->
          ``armed``; else ``db-only`` -- the recorded stop exists only as a DB
          number. A REAL/manual row is NEVER ``unprotected``: the venue does not
          know it exists, so ``db-only`` is its honest ceiling. ``unprotected``
          is reserved for a row with no recorded stop at all (both stop columns
          are NOT NULL today, so it is a wire-contract state, not a live one).
        * CAPS mirror ``execution._limit_block``'s reads: ``notional`` sums
          ``ExecutionLog.notional`` over ``execution_logs_for_day`` (the
          REPO filters to the counting statuses -- skipped/canceled/rejected
          never reserved notional); ``loss_r`` is ``realized_r_on`` -- an R
          THRESHOLD, today's realized R with sign preserved (the breaker fires
          at ``used <= -limit``), NOT a spent-dollars meter; ``concurrent``
          under a REAL execution mode (manual/paper/live -- where the adapter
          genuinely enforces the per-account cap) is ``count_open_positions``
          (PaperTrade rows only -- exactly what the adapter checks; open manual
          Trades don't count there either). Under mode ``off`` (the default)
          NOTHING enforces anything and ``OFF_ACCOUNT`` is merely the research
          LABEL, so the adapter read would count the open research SHADOW GRID
          -- potentially hundreds of rows with zero corresponding trades on
          this screen; ``concurrent.used`` therefore counts what the screen
          DISPLAYS (the open rows above: open manual Trades + open live
          PaperTrades). ``mode`` (ADDITIVE, 2026-07) carries the execution mode
          and ``account`` keeps its adapter-constant label (off -> the research
          label, unchanged) so the frontend can caption the gauge honestly;
          every pre-existing field keeps its meaning for non-off modes.
          ``run_date = latest_run_date``; with no runs yet the day-scoped used
          values are an honest 0.0 and ``run_date`` null. A ``None`` cap is
          unbounded: ``limit: null`` on the wire, NEVER 0/0.
        * ``closed`` arrives newest-exit first (the repo's order); ``equity`` is
          the retired Streamlit ``_render_closed`` math verbatim: dated closes
          ascending, running sum of ``((exit or entry) - entry) * size`` rounded
          to cents, an undated close listed but never plotted.
        * ONE ``QuoteCache.get`` for all open tickers per request (the TTL cache
          makes a cold fetch at most once per window); ``quotes_as_of``
          timestamps the window's latest contribution, not each price.
          ``broker_as_of`` is null without a snapshot.

        Budget: cheap per request; the outlier is the cold quote fetch --
        multi-second yfinance under the single-flight lock, at most once per
        600s window.
        """
        open_real = get_open_trades(session)
        open_live = load_open_live_trades(session)
        quote_result = quote_cache.get(
            [t.ticker for t in open_real] + [p.ticker for p in open_live])
        prices = quote_result.prices
        snapshot = broker_snapshot.get()
        armed = _armed_symbols(snapshot)

        open_rows: list[dict[str, object]] = [
            _real_position_row(t, prices.get(t.ticker), snapshot=snapshot, armed=armed)
            for t in open_real
        ]
        open_rows += [
            _live_position_row(p, prices.get(p.ticker),
                               # VENUE TRUTH FIRST: the reconciler stamps ``qty`` from
                               # the broker's filled_qty, so a partial fill sizes the
                               # tile at what is actually held -- the ticket's REQUESTED
                               # shares would overstate it. The ExecutionLog join is the
                               # legacy fallback only (``qty`` is NULL on rows booked
                               # before the column existed); it also saves the per-row
                               # query on every modern row.
                               p.qty if p.qty is not None
                               else _live_shares(session, p.ticker),
                               snapshot=snapshot, armed=armed)
            for p in open_live
        ]

        mode, limits = resolve_execution(load_settings())
        account = _ACCOUNT_FOR_MODE[mode]  # total: load_settings coerces unknown->off
        run_d = latest_run_date(session)
        if run_d is None:  # no runs yet: no day to sum -- honest zeros, null date
            notional_used, loss_used = 0.0, 0.0
        else:
            notional_used = sum(e.notional for e in execution_logs_for_day(
                session, run_date=run_d, account=account))
            if mode == "off":
                # HONESTY (the same 2026-07 rule as `concurrent` below): with
                # execution off no adapter enforces the loss cap and OFF_ACCOUNT
                # is the research LABEL -- realized_r_on over it sums the
                # invisible shadow grid's closes as 'loss used'. There is no
                # enforced book to sum, so the gauge reads an honest 0.
                loss_used = 0.0
            else:
                loss_used = realized_r_on(session, run_date=run_d, account=account)
        if mode == "off":
            # HONESTY (2026-07 usability finding): with execution off no adapter
            # enforces a cap and OFF_ACCOUNT is the research LABEL -- counting
            # that account here rendered "N concurrent positions used" off the
            # invisible shadow grid. Count what THIS screen displays instead.
            concurrent_used = len(open_rows)
        else:
            concurrent_used = count_open_positions(session, account=account)

        closed_trades = get_closed_trades(session)
        dated = sorted((t for t in closed_trades if t.exit_date is not None),
                       key=lambda t: cast(date, t.exit_date))
        equity: list[list[object]] = []
        running = 0.0
        for t in dated:
            running += _realized_usd(t)
            equity.append([cast(date, t.exit_date).isoformat(), round(running, 2)])

        return {
            "open": open_rows,
            "caps": {
                "mode": mode,
                "account": account,
                "run_date": run_d.isoformat() if run_d is not None else None,
                "notional": {"used": notional_used,
                             "limit": limits.max_daily_notional},
                "loss_r": {"used": loss_used, "limit": limits.max_daily_loss},
                "concurrent": {
                    "used": concurrent_used,
                    "limit": limits.max_concurrent,
                },
            },
            "closed": [{
                "trade_id": t.id,
                "ticker": t.ticker,
                "entry_date": t.entry_date.isoformat(),
                "exit_date": (t.exit_date.isoformat()
                              if t.exit_date is not None else None),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "size": t.size,
                "realized_usd": _realized_usd(t),
                "exit_reason": t.exit_reason,
            } for t in closed_trades],
            "equity": equity,
            "quotes_as_of": quote_result.as_of.isoformat(),
            "broker_as_of": (snapshot.as_of.isoformat()
                             if snapshot is not None else None),
        }

    @router.get("/api/trade-defaults")
    def trade_defaults(
        signal_id: int, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """The log-trade form's prefill for one Signal: the engine's levels
        VERBATIM (never recomputed -- the analyst/UI can never move a level),
        live actionability, a suggested entry, and the conviction-'medium' size.

        404 on an unknown ``signal_id`` (missing/garbage is FastAPI's 422).
        ``actionability`` classifies the entry zone at the cached quote
        (``signals.actionability.classify``) and is TOTAL -- the /api/picks wire
        form: a missing quote reads ``{"status": "unknown", "dist_r": null}``,
        never null, so the frontend has ONE actionability shape everywhere.
        ``suggested_entry`` stays null without a quote; with one it is the quote
        CLAMPED into ``[entry_floor, entry_ceiling]``: the prefill never chases
        an extended price above the ceiling nor bids below the zone. ``sizing``
        is ``insight.size_order`` at conviction 'medium' over
        ``resolve_risk_unit(load_settings())``; ``shares == 0`` is the deliberate
        'sizing unconfigured' signal (``unconfigured: true`` -- the UI renders
        R-multiples, never a guessed dollar). One ``QuoteCache.get`` per request.
        """
        sig = session.get(Signal, signal_id)
        if sig is None:
            raise HTTPException(
                status_code=404, detail=f"no signal with id {signal_id}")
        price = quote_cache.get([sig.ticker]).prices.get(sig.ticker)
        result = classify(entry_floor=sig.entry_floor,
                          entry_ceiling=sig.entry_ceiling,
                          stop=sig.stop, price=price)
        actionability = {"status": result.status, "dist_r": result.dist_r}
        suggested: float | None = None
        if price is not None:
            suggested = min(max(price, sig.entry_floor), sig.entry_ceiling)
        risk_unit, max_shares = resolve_risk_unit(load_settings())
        shares, risk_dollars = size_order(
            conviction="medium", entry_ceiling=sig.entry_ceiling, stop=sig.stop,
            risk_unit_dollars=risk_unit, max_shares=max_shares)
        return {
            "signal": {
                "ticker": sig.ticker,
                "timeframe": sig.timeframe,
                "horizon": sig.horizon,
                "play_type": sig.play_type,
                "entry_floor": sig.entry_floor,
                "entry_ceiling": sig.entry_ceiling,
                "stop": sig.stop,
                "target": sig.target,
                "conviction_tier": sig.conviction_tier,
            },
            "last_close": price,
            "actionability": actionability,
            "suggested_entry": suggested,
            "sizing": {"shares": shares, "risk_dollars": risk_dollars,
                       "unconfigured": shares == 0},
        }

    return router


# Execution mode -> the account its adapter books under (execution.py's constants;
# "off" books nothing and carries the research label purely as a label). Total over
# the mode enum: load_settings coerces any unknown mode to "off" before it gets here.
_ACCOUNT_FOR_MODE = {
    "manual": MANUAL_ACCOUNT,
    "paper": PAPER_ACCOUNT,
    "live": LIVE_ACCOUNT,
    "off": OFF_ACCOUNT,
}


def _badge(price: float, *, stop: float, target: float) -> str:
    """The attention lamp for a priced open row: ``red`` at/under the stop (the
    exit case), ``yellow`` at/over the target (the take-profit case), ``green``
    between. The no-price/unusable-geometry ``unknown`` is the CALLER's branch --
    this helper only speaks when there is a price to compare."""
    if price <= stop:
        return "red"
    if price >= target:
        return "yellow"
    return "green"


def _pl_dict(pl: PositionPL) -> dict[str, object]:
    """``PositionPL``'s wire form, hand-rolled like ``_beat_dict`` (never
    ``dataclasses.asdict`` on the wire). ``unrealized_pct`` is a FRACTION."""
    return {
        "unrealized_pl": pl.unrealized_pl,
        "unrealized_pct": pl.unrealized_pct,
        "r_multiple": pl.r_multiple,
        "dist_to_stop_pct": pl.dist_to_stop_pct,
        "dist_to_target_pct": pl.dist_to_target_pct,
    }


def _real_position_row(t: Trade, price: float | None, *,
                       snapshot: Snapshot | None,
                       armed: frozenset[str]) -> dict[str, object]:
    """One REAL (manual) open-trade row. The per-row guard: ``position_pl`` raises
    ``ValueError`` on non-positive risk and a zero price would ZeroDivision the
    distance math -- either degrades THIS row to ``pl: null`` and the row is KEPT
    (one malformed trade never 503s the zone). The badge is computed BEFORE that
    try: it reads only price/stop/target, so a legacy malformed-entry row whose
    price breached the stop still shows RED, never 'unknown' -- that lamp's job is
    'get out'. ``last_close`` likewise stays whatever the quote said: the close
    form prefills from it even when the P/L math is broken."""
    pl: dict[str, object] | None = None
    badge = "unknown"
    if price is not None:
        badge = _badge(price, stop=t.stop, target=t.target)
        try:
            pl = _pl_dict(position_pl(entry=t.entry_price, stop=t.stop,
                                      target=t.target, size=t.size,
                                      current_price=price))
        except (ValueError, ZeroDivisionError):
            pl = None  # the P/L math is untrustable; the badge above still stands
    return {
        "kind": "real",
        "trade_id": t.id,
        "ticker": t.ticker,
        "timeframe": t.timeframe,
        "entry_price": t.entry_price,
        "size": t.size,
        "stop": t.stop,
        "target": t.target,
        "last_close": price,
        "pl": pl,
        "badge": badge,
        "bracket": _bracket(t.ticker, t.stop, snapshot=snapshot, armed=armed),
        "override": t.override,
        "signal_id": t.signal_id,
        "unlinked": t.signal_id is None,
    }


def _live_shares(session: Session, ticker: str) -> int | None:
    """The NEWEST live ENTRY ticket's share count for ``ticker``, or None -- the
    LEGACY fallback for a live row whose ``qty`` is NULL (rows booked before the
    column existed). Modern rows size off ``PaperTrade.qty`` instead, because this
    join answers with what the ticket REQUESTED, not what the venue filled.
    Only ``submitted_live`` /
    ``filled_live`` rows count (the statuses that created venue exposure --
    canceled/rejected tickets never did), and only the ENTRY side (a sell-side live
    ticket is an EXIT; its shares must never masquerade as position size);
    newest-by-id mirrors ``repo.latest_recorded_stop``'s ordering AND filters --
    literally, via the shared ``repo.ENTRY_SIDES``, whose docstring records why
    matching the venue's "buy" alone made this join dead on production rows.
    One query per open live row; batch (windowed IN) if the live book grows."""
    stmt = (
        select(ExecutionLog.shares)
        .where(
            ExecutionLog.ticker == ticker,
            ExecutionLog.side.in_(ENTRY_SIDES),
            ExecutionLog.status.in_(("submitted_live", "filled_live")),
        )
        .order_by(ExecutionLog.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _live_grades(
    price: float, *, entry: float, risk: float, stop: float, target: float
) -> dict[str, float | None]:
    """The size-independent live-grading math for one OPEN paper position at
    ``price`` -- the ONE home shared by ``_live_position_row`` (the positions
    screen's live rows) and the books router's ``/api/books/open`` rows, so the
    two surfaces can never drift. Long-only book: every sign reads long. Each
    denominator is guarded per FIELD (risk/entry/price non-positive -> that
    field null) -- the shared never-503 posture. Keep the pct fields in
    lockstep with ``analytics.pl.PositionPL`` (the real rows' source, via
    ``_pl_dict``); the ``to_*_r`` pair is the same distance math in R units:
    negative ``to_stop_r`` = the stop is breached, negative ``to_target_r`` =
    the target is overshot."""
    return {
        "unrealized_pct": (price - entry) / entry if entry > 0 else None,
        "r_multiple": (price - entry) / risk if risk > 0 else None,
        "dist_to_stop_pct": (price - stop) / price if price > 0 else None,
        "dist_to_target_pct": (target - price) / price if price > 0 else None,
        "to_stop_r": (price - stop) / risk if risk > 0 else None,
        "to_target_r": (target - price) / risk if risk > 0 else None,
    }


def _live_position_row(p: PaperTrade, price: float | None, shares: int | None, *,
                       snapshot: Snapshot | None,
                       armed: frozenset[str]) -> dict[str, object]:
    """One LIVE (broker-owned) open-position row. ``size`` is the caller's
    resolved share count -- ``PaperTrade.qty`` (VENUE TRUTH: the broker's
    ``filled_qty``, stamped by ``reconcile._materialize_fills``) when set, else the
    ExecutionLog join (``_live_shares``) for legacy NULL-``qty`` rows -- or null;
    without it the dollar P/L is null while the R-multiple still renders from the
    persisted per-share ``risk`` and the size-independent percent fields stay
    honest. Never the ticket's REQUESTED shares when a fill is on record: a
    partially filled entry holds fewer shares than it asked for, and the dollar
    P/L on this tile must not overstate the position. A pending entry
    (``entry_price`` null) carries ``pl: null``; the badge still reads off the
    quote (stop/target are always recorded). The size-independent fields come
    from ``_live_grades`` (shared with /api/books/open -- one home for the
    math); only the shares-dependent dollar P/L is computed here. Live rows
    have no ``override`` column: null, with ``unlinked`` still keyed off
    ``signal_id`` (a reconciler-materialized fill carries none)."""
    pl: dict[str, object] | None = None
    badge = "unknown"
    if price is not None:
        badge = _badge(price, stop=p.stop, target=p.target)
        if p.entry_price is not None:
            entry = p.entry_price
            g = _live_grades(price, entry=entry, risk=p.risk,
                             stop=p.stop, target=p.target)
            pl = {
                "unrealized_pl": ((price - entry) * shares
                                  if shares is not None else None),
                "unrealized_pct": g["unrealized_pct"],
                "r_multiple": g["r_multiple"],
                "dist_to_stop_pct": g["dist_to_stop_pct"],
                "dist_to_target_pct": g["dist_to_target_pct"],
            }
    return {
        "kind": "live",
        "paper_id": p.id,
        "ticker": p.ticker,
        "timeframe": p.timeframe,
        "entry_price": p.entry_price,
        "size": float(shares) if shares is not None else None,
        "stop": p.stop,
        "target": p.target,
        "last_close": price,
        "pl": pl,
        "badge": badge,
        "bracket": _bracket(p.ticker, p.stop, snapshot=snapshot, armed=armed),
        "override": None,
        "signal_id": p.signal_id,
        "unlinked": p.signal_id is None,
    }


def _realized_usd(t: Trade) -> float:
    """The retired Streamlit ``_render_closed`` realized math, verbatim: an
    exit-less close falls back to the entry price (realized 0.0), never a guess."""
    exit_price = t.exit_price if t.exit_price is not None else t.entry_price
    return (exit_price - t.entry_price) * t.size
