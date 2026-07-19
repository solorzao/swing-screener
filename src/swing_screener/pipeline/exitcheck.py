"""Intraday exit checker for REAL trades -- plus the hourly live-book sync.

The shadow book (``pipeline/shadow.py``) already advances *paper* trades and
records ``is_paper=True`` exit events. But the exit-alert email reads
``is_paper=False`` events, so without a producer for real trades the
exit-alert path is a permanent no-op. This module is that producer: it walks
every open real :class:`Trade`, asks the *same* :func:`evaluate_exit` whether
the latest bar trips an exit, and records an ``is_paper=False`` ExitEvent for
each one that does -- WITHOUT closing the trade (a human closes trades in the
dashboard; this only ALERTS).

The bar source is an injectable seam (``latest_bars_fn``) so the whole thing
runs offline in tests. The default seam mirrors ``pipeline/run.py``'s live
fetch -> enrich -> last-bar shape, including the HA ``shaved_head`` flag that
``evaluate_exit`` reads.

Because this is the ONLY intraday job, it also carries the hourly live-book
sync (Task 11), appended after the exit walk: (a) the shared live reconcile
(``live_sync.maybe_reconcile_live`` -- a broker fill materializes the same
hour, a venue stop-out books its realized $ within the hour); (b) the
guardrail sweep-resume hoist (state-check BEFORE any broker build, broker on
demand REGARDLESS of the execution mode -- a crashed trip sweep is retried
within the hour, not at the next digest); (c) the shared guardrails consult,
so a loss the reconcile just booked trips the breakers same-hour; (d) the
AT-LEAST-ONCE alert retries -- query-based live-rejection alerts (rows with no
per-row EmailLog coverage; the digest's failed sends are otherwise lost) and
the pending guardrail-trip alert. Every phase is swallow-everything with a
rollback-first except: the job's core product (the exit events the alert email
reads) must never be blocked by live-book machinery. Alert transports resolve
LAZILY at call time (``notify.transport``/``notify.alerts`` are call-time
imports -- notify.run imports THIS module at module level, so the import edge
must stay one-way; the fresh-interpreter canary in
tests/pipeline/test_exitcheck_live.py pins the module-import surface).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import guardrails_repo, repo
from swing_screener.db.models import Trade
from swing_screener.db.session import get_engine
from swing_screener.pipeline import guardrails as gpipe
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.live_sync import maybe_reconcile_live
from swing_screener.settings import load_settings
from swing_screener.signals.exits import OpenTrade, evaluate_exit

log = logging.getLogger(__name__)

# A per-ticker bar in the exact shape evaluate_exit consumes (mirrors shadow.py /
# pipeline.run._bar_row): low/high/shaved_head are read by evaluate_exit;
# close/bearish ride along for parity with the shadow book's bar.
Bar = Mapping[str, float | bool]
LatestBarsFn = Callable[[list[str], str], dict[str, Bar]]

_BAR_KEYS = ("low", "high", "close", "shaved_head", "bearish")

# 4h frames are resampled from 1h bars (~2 four-hour buckets per US session), so
# this is the only timeframe whose bar count is not exactly date-derivable.
_BARS_PER_TRADING_DAY_4H = 2


@dataclass(frozen=True)
class ExitCheckResult:
    n_open: int
    n_exited: int


def _bars_held(timeframe: str, entry_date: date, today: date) -> int:
    """Number of ``timeframe`` bars elapsed between entry and today.

    ``evaluate_exit``'s time-stop compares ``bars_held`` to
    ``max_hold_bars[timeframe]``, which is denominated in that timeframe's OWN
    bars (weeks for ``1wk``, months for ``1mo``, trading days for ``1d``). The
    daily/weekly/monthly frames are date-aligned, so the count is exact from the
    dates; ``4h`` is intraday and only approximable. Using a raw calendar-day
    count here would trip the weekly/monthly time-stop ~7-30x too early.
    """
    if today <= entry_date:
        return 0
    if timeframe == "1wk":
        return (today - entry_date).days // 7
    if timeframe == "1mo":
        return (today.year - entry_date.year) * 12 + (today.month - entry_date.month)
    trading_days = int(np.busday_count(entry_date, today))
    if timeframe == "4h":
        return trading_days * _BARS_PER_TRADING_DAY_4H
    return trading_days  # "1d" (one bar per trading day) and any daily-aligned default


def _live_latest_bars(tickers: list[str], timeframe: str) -> dict[str, Bar]:
    """Default live seam: fetch + enrich each ticker and return its last bar.

    Mirrors ``pipeline.run`` (fetch_bars -> build_frame -> last row over
    ``_BAR_KEYS``). Per-ticker isolated: a ticker that fails to fetch is simply
    absent from the result. Not exercised in tests (they inject a fake).
    """
    from swing_screener.data.fetch import fetch_bars
    from swing_screener.signals.frame import build_frame

    cfg = StrategyConfig()
    cache_dir = Path(".cache")
    out: dict[str, Bar] = {}
    for ticker in tickers:
        df = fetch_bars(ticker, timeframe, cache_dir=cache_dir)
        if df is None or df.empty:
            continue
        frame = build_frame(df, cfg)
        if not len(frame):
            continue
        last = frame.iloc[-1]
        out[ticker] = {k: last[k] for k in _BAR_KEYS}
    return out


def _bars_for(trades: list[Trade], latest_bars_fn: LatestBarsFn) -> dict[tuple[str, str], Bar]:
    """Resolve the latest bar for each open trade, grouped by timeframe.

    Open trades can span timeframes, so the seam is queried once per timeframe
    with that timeframe's tickers. Keyed by (ticker, timeframe) to disambiguate
    a ticker held on two timeframes.
    """
    by_tf: dict[str, list[str]] = {}
    for t in trades:
        by_tf.setdefault(t.timeframe, []).append(t.ticker)

    bars: dict[tuple[str, str], Bar] = {}
    for timeframe, tickers in by_tf.items():
        for ticker, bar in latest_bars_fn(tickers, timeframe).items():
            bars[(ticker, timeframe)] = bar
    return bars


def _rollback_guarded(session: Session) -> None:
    """Best-effort rollback after a failed live-sync phase: a failure can leave
    the SHARED session's transaction poisoned (PendingRollbackError on every
    later use) -- and the phases after it, plus the caller's exit-alert email
    path, still need it (the dispatch loop's hardened posture)."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 -- the exit-check result is the priority
        log.warning("hourly live-sync rollback failed", exc_info=True)


def _env_trip_emailer(session: Session, *, run_date: date) -> Callable[[int, str, str], None]:
    """The hourly job's minimal ``respond_to_trip`` emailer -- built lazily
    exactly like ``pipeline.run._screen_trip_emailer`` (this job has no digest
    transport in scope either): recipient from the DIGEST_TO secret, sender
    from ``notify.transport.resolve_sender``, and the send-then-log body from
    the SHARED ``notify.alerts.send_guardrail_alert`` -- the one owner of the
    ``alert_key=str(trip_event_id)`` dedup contract, so whichever path fires
    first wins and the others no-op. Everything imported at CALL time (the
    module docstring's one-way import edge). No recipient -> skip with a log;
    the digest/hourly retry emitters own the alert."""
    def _emailer(trip_event_id: int, breaker: str, reason: str) -> None:
        from swing_screener.config_secrets import get_secret
        from swing_screener.notify.alerts import send_guardrail_alert
        from swing_screener.notify.transport import resolve_sender

        recipient = get_secret("DIGEST_TO")
        if not recipient:
            log.warning("guardrail trip %d: DIGEST_TO not configured; alert email "
                        "deferred to the retry emitters", trip_event_id)
            return
        send_guardrail_alert(session, run_date=run_date, recipient=recipient,
                             send=resolve_sender(), trip_event_id=trip_event_id,
                             breaker=breaker, reason=reason)
    return _emailer


def _resolve_alert_transport() -> tuple[str, Callable[..., None]] | None:
    """``(recipient, send)`` for the hourly retry emitters, or None to skip.

    Resolved lazily and only when there is something to send (both call sites
    query first): recipient from the DIGEST_TO secret, transport from
    ``notify.transport.resolve_sender`` (ACS in prod, SMTP fallback) -- imported
    at CALL time (the module docstring's one-way import edge). Missing
    recipient degrades to a logged skip, mirroring ``_env_trip_emailer``."""
    from swing_screener.config_secrets import get_secret
    from swing_screener.notify.transport import resolve_sender

    recipient = get_secret("DIGEST_TO")
    if not recipient:
        log.warning("DIGEST_TO not configured; hourly alert retry skipped this cycle")
        return None
    return recipient, resolve_sender()


def _hourly_live_sync(session: Session, *, today: date,
                      broker: BrokerClient | None = None) -> None:
    """The Task-11 hourly live-book pass (see the module docstring's (a)-(d)).

    Each phase is isolated swallow-everything + rollback-first: protection and
    alerting are best-effort, the exit walk's committed events (and the alert
    email the caller sends from them) must always survive. ``broker`` is the
    test seam; prod resolves brokers on demand (``maybe_reconcile_live``'s
    guarded build, and the resume hoist's mode-independent build).
    """
    # (a) the shared live reconcile: a fill materializes / a venue close books
    # its realized $ THIS hour. The returned broker (possibly built on demand
    # for the disarmed-exposure path) is kept for the consult below.
    try:
        _n_changes, broker = maybe_reconcile_live(session, today=today, broker=broker)
    except Exception:  # noqa: BLE001 -- the live book must not break the exit job
        log.warning("hourly live reconcile failed", exc_info=True)
        _rollback_guarded(session)
    # (b) GUARDRAIL SWEEP RESUME -- the digest hoist, mirrored exactly
    # (notify.run.send_digest's once-per-run resume): state check BEFORE any
    # broker build, broker on demand REGARDLESS of the execution mode, so a
    # crashed trip sweep (or one stranded by the operator flipping the mode
    # off -- the natural post-trip reaction) is retried within the hour.
    try:
        g0 = guardrails_repo.load_guardrails(session)
        if g0.state == "tripped" and g0.sweep_state in ("pending", "partial"):
            resume_broker = broker or build_broker(load_settings())
            gpipe.resume_incomplete_sweep(session, broker=resume_broker,
                                          source="exitcheck")
            broker = broker or resume_broker  # reuse for the consult below
    except Exception:  # noqa: BLE001 -- the sweep resume must never block the exit job
        log.warning("hourly guardrail sweep resume failed", exc_info=True)
        _rollback_guarded(session)
    # (c) the shared consult (Task 11): the reconcile above may have just
    # booked a stop-out's realized loss -- trip the breakers same-HOUR, not at
    # the evening screen. Bare call, verdict ignored: this job dispatches
    # nothing, so a halted/tripped book needs no entry-pull from here (the
    # digest's dispatch loop owns that response). broker None -> a fresh trip
    # persists with sweep_state='pending' and the next cycle's hoist (b)
    # finishes it.
    try:
        gpipe.consult(session, run_date=today, source="exitcheck", broker=broker,
                      emailer=_env_trip_emailer(session, run_date=today))
    except Exception:  # noqa: BLE001 -- guardrails must never block the exit job
        log.warning("hourly guardrails consult failed", exc_info=True)
        _rollback_guarded(session)
    # (d) AT-LEAST-ONCE alert retries. Live rejections: QUERY-based (rows with
    # no per-row EmailLog coverage), NOT the digest's before/after diff -- a
    # digest send that failed leaves ids the diff never re-produces, so THIS
    # pass is the retry owner. The per-row 'xlog-{id}' keys make a partial
    # overlap alert only the uncovered rows (notify.alerts owns the contract).
    try:
        from swing_screener.notify import alerts as notify_alerts

        pending = notify_alerts.pending_rejection_ids(session, run_date=today)
        if pending:
            transport = _resolve_alert_transport()
            if transport is not None:
                recipient, send = transport
                notify_alerts.send_live_rejection_alert(
                    session, run_date=today, recipient=recipient, send=send,
                    candidate_ids=pending)
    except Exception:  # noqa: BLE001 -- alerting must never block the exit job
        log.warning("hourly live-rejection alert retry failed", exc_info=True)
        _rollback_guarded(session)
    # ...and the guardrail-trip retry: a tripped book whose alert never landed
    # (the screen's transport-less trip, a dead SMTP) is mailed within the
    # hour. The emitter state-checks and dedups internally; the pre-check here
    # only avoids resolving a transport when there is nothing to send.
    try:
        from swing_screener.notify import alerts as notify_alerts

        g1 = guardrails_repo.load_guardrails(session)
        if (g1.state == "tripped" and g1.trip_id is not None
                and not notify_alerts.guardrail_alert_sent(session, str(g1.trip_id))):
            transport = _resolve_alert_transport()
            if transport is not None:
                recipient, send = transport
                notify_alerts.emit_pending_guardrail_alert(
                    session, today, recipient, send)
    except Exception:  # noqa: BLE001 -- alerting must never block the exit job
        log.warning("hourly guardrail-trip alert retry failed", exc_info=True)
        _rollback_guarded(session)


def run_exit_check(*, db_url: str, today: date | None = None,
                   latest_bars_fn: LatestBarsFn = _live_latest_bars,
                   broker: BrokerClient | None = None) -> ExitCheckResult:
    """Check every open real trade against its latest bar; alert on exits.

    For each open :class:`Trade` we build an :class:`OpenTrade` exactly the way
    ``shadow.advance_open`` builds one for a paper trade, call the shared
    :func:`evaluate_exit`, and on an EXIT decision record an ``is_paper=False``
    ExitEvent (deduped by ``(trade_id, reason, created_date)`` so an hourly
    re-run is idempotent). The real ``Trade`` row is never mutated.

    After the walk commits, the hourly live-book sync runs (Task 11 -- see the
    module docstring): reconcile, sweep resume, guardrails consult, and the
    at-least-once alert retries -- all best-effort, never blocking this
    function's result. ``broker`` is the injectable test seam (mirrors
    ``run_screen``'s); prod leaves it None and builds on demand.
    """
    today = today or date.today()
    cfg = StrategyConfig()
    engine = get_engine(db_url)

    n_exited = 0
    with Session(engine) as session:
        open_trades = repo.get_open_trades(session)
        bars = _bars_for(open_trades, latest_bars_fn)

        # play_type selects the exit POLICY: the momentum-flip exit is a net drag on
        # reversals (config.reversal_momentum_flip_exit, default off for that book), so
        # a real reversal trade must not be alerted to flip-exit like a continuation.
        # A Trade row doesn't carry play_type; resolve it through the signal_id FK in
        # one batch. Legacy/manual rows with no signal fall back to "continuation"
        # (the historical behavior).
        play_types = repo.signal_play_types(
            session, [t.signal_id for t in open_trades if t.signal_id is not None])

        # Dedupe against today's already-recorded real exit events so a re-run of
        # the hourly job never piles up duplicates.
        seen = {
            (ev.trade_id, ev.reason)
            for ev in repo.exit_events_for(session, today, is_paper=False)
        }

        for t in open_trades:
            bar = bars.get((t.ticker, t.timeframe))
            if bar is None:
                continue

            # Map the real Trade -> OpenTrade just like shadow.advance_open.
            # bars_held is the count of THIS timeframe's bars since entry (shadow
            # bumps hold_bars each advance; for a real trade we derive it from the
            # entry date), so the time-stop fires at the right horizon per frame.
            bars_held = _bars_held(t.timeframe, t.entry_date, today)
            open_trade = OpenTrade(
                entry=t.entry_price,
                stop=t.stop,
                target=t.target,
                timeframe=t.timeframe,
                bars_held=bars_held,
                play_type=(play_types.get(t.signal_id, "continuation")
                           if t.signal_id is not None else "continuation"),
            )
            decision = evaluate_exit(open_trade, bar, cfg)
            if decision.action != "EXIT":
                continue

            if (t.id, decision.reason or "") in seen:
                continue  # already alerted on this exit today
            seen.add((t.id, decision.reason or ""))

            repo.record_exit_event(
                session,
                is_paper=False,
                trade_id=t.id,
                tier=decision.tier or "",
                reason=decision.reason or "",
                message=f"{t.ticker} {t.timeframe} {decision.reason}",
                created_date=today,
            )
            n_exited += 1

        session.commit()

        # The hourly live-book sync (Task 11) -- AFTER the commit, so no
        # uncommitted exit events are ever pending when guardrails machinery
        # commits/rolls back on the shared session.
        _hourly_live_sync(session, today=today, broker=broker)

        return ExitCheckResult(n_open=len(open_trades), n_exited=n_exited)
