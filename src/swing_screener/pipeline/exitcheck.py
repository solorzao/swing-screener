"""Intraday exit checker for REAL trades.

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
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import Trade
from swing_screener.db.session import get_engine
from swing_screener.signals.exits import OpenTrade, evaluate_exit

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


def run_exit_check(*, db_url: str, today: date | None = None,
                   latest_bars_fn: LatestBarsFn = _live_latest_bars) -> ExitCheckResult:
    """Check every open real trade against its latest bar; alert on exits.

    For each open :class:`Trade` we build an :class:`OpenTrade` exactly the way
    ``shadow.advance_open`` builds one for a paper trade, call the shared
    :func:`evaluate_exit`, and on an EXIT decision record an ``is_paper=False``
    ExitEvent (deduped by ``(trade_id, reason, created_date)`` so an hourly
    re-run is idempotent). The real ``Trade`` row is never mutated.
    """
    today = today or datetime.now(UTC).date()
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
        return ExitCheckResult(n_open=len(open_trades), n_exited=n_exited)
