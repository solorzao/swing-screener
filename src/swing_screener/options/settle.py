"""Worst-case settlement of open options-lab paper trades from completed 5m bars.

The nightly job feeds the day's completed 5-minute bars per underlying; each open
lab trade is walked forward from its open time and closed at the first structural
touch. Conventions (pessimistic, matching the equity book):
  * A bar "touches" a level by high/low range inclusion.
  * Long: stop = low <= stop, target = high >= target. Short: mirrored.
  * Both levels inside one bar -> stop wins (we cannot see intrabar order, so we
    assume the adverse fill).
  * Neither level touched across a COMPLETE session -> flat at the last bar's
    close. When the frame's session is still in progress (or predates the trade's
    session day), the trade stays OPEN instead -- a closed status is immutable
    (journal.SettledTradeError), so an intraday eod_flat would permanently
    mis-grade the trade at a mid-session price (2026-07-17 audit, H2).
Stop/target fills land AT the level (structural, not an intrabar guess) and are
NOT gated on session completeness -- a stop that genuinely traded settles.
Imported robinhood trades never settle here -- their book is reconstructed from
fills.
"""

import logging
from dataclasses import dataclass
from datetime import time

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade

log = logging.getLogger(__name__)

# A 5m frame whose final bar starts at/after 15:55 ET spans the 16:00 close, so its
# last close is a true session-close price -- safe for the eod_flat fallback.
# TODO(2026-07-17 audit, H2): like data/fetch.py's close guard, this ignores half-days
# (a 1pm close leaves trades open until the next full session's bars land -- they then
# eod_flat at the NEXT session's close). Doing it right needs a market calendar.
_LAST_SESSION_BAR = time(15, 55)


@dataclass
class SettleResult:
    settled: int
    skipped_no_bars: int
    skipped_incomplete_session: int = 0


def settle_open_trades(
    session: Session, *, bars_by_underlying: dict[str, pd.DataFrame]
) -> SettleResult:
    """Close every open options-lab trade against its underlying's completed bars.

    Trades whose underlying has no frame are left open (retried on the next run),
    as are untouched trades whose frame does not cover a complete session for the
    trade's session day (counted in ``skipped_incomplete_session`` -- only the
    eod_flat fallback is gated; intraday stop/target touches always settle).
    Commits at the end, repo-style.
    """
    stmt = select(OptionPaperTrade).where(
        OptionPaperTrade.account == "options-lab",
        OptionPaperTrade.status == "open",
        OptionPaperTrade.entry.is_not(None),
        OptionPaperTrade.stop.is_not(None),
        OptionPaperTrade.target.is_not(None),
    )
    settled = 0
    skipped_no_bars = 0
    skipped_incomplete_session = 0
    for trade in session.scalars(stmt):
        frame = bars_by_underlying.get(trade.underlying)
        if frame is None or frame.empty:
            skipped_no_bars += 1
            log.warning("no bars for %s; leaving trade %s open", trade.underlying, trade.id)
            continue

        # Normalize the index to naive US/Eastern -- yfinance 5m bars are tz-aware,
        # test fixtures are naive. Copy only when a conversion is needed so the
        # caller's frame is never mutated.
        idx = frame.index
        if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
            frame = frame.copy()
            frame.index = idx.tz_convert("America/New_York").tz_localize(None)

        # Bars from the open onward. If the trade opened after every available bar
        # (all bars precede it), fall back to the full session so a same-bar entry
        # still settles rather than lingering open forever.
        relevant = frame
        if trade.opened_at is not None:
            after_open = frame[frame.index >= trade.opened_at]
            if not after_open.empty:
                relevant = after_open

        # eod_flat fills at the frame's LAST close, so that close must be a real
        # session-close price (last bar at/after 15:55) on a day no earlier than
        # the trade's session day (a stale prior-day frame must not flatten
        # today's trade at yesterday's close).
        last_ts = pd.Timestamp(frame.index[-1])
        session_day = trade.opened_at.date() if trade.opened_at is not None else last_ts.date()
        session_complete = (last_ts.date() >= session_day
                            and last_ts.time() >= _LAST_SESSION_BAR)

        if _close_trade(trade, relevant, allow_eod_flat=session_complete):
            settled += 1
        else:
            skipped_incomplete_session += 1
            log.info("session incomplete for %s (last bar %s); leaving trade %s open",
                     trade.underlying, last_ts, trade.id)

    session.commit()
    return SettleResult(settled=settled, skipped_no_bars=skipped_no_bars,
                        skipped_incomplete_session=skipped_incomplete_session)


def _close_trade(trade: OptionPaperTrade, bars: pd.DataFrame, *,
                 allow_eod_flat: bool = True) -> bool:
    """Walk the bars and close the trade at the first structural touch. Returns
    True when the trade was closed; False when nothing touched and the eod_flat
    fallback is disallowed (incomplete session) -- the trade is left untouched."""
    entry = float(trade.entry)  # type: ignore[arg-type]
    stop = float(trade.stop)  # type: ignore[arg-type]
    target = float(trade.target)  # type: ignore[arg-type]
    is_long = trade.direction == "long"

    # Typed column arrays + a plain timestamp list keep the bar-walk clear of
    # pandas' loosely-typed itertuples rows (mypy can't prove those are floats).
    lows = bars["low"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    timestamps = list(bars.index)

    exit_reason: str | None = None
    exit_price = 0.0
    closed_ts = timestamps[-1]          # bars is non-empty; eod_flat closes here
    last_close = float(closes[-1])
    for i in range(len(timestamps)):
        low, high = float(lows[i]), float(highs[i])
        if is_long:
            stop_hit, target_hit = low <= stop, high >= target
        else:
            stop_hit, target_hit = high >= stop, low <= target
        # Both in one bar -> stop (worst-case): check the stop first.
        if stop_hit:
            exit_reason, exit_price, closed_ts = "stop", stop, timestamps[i]
            break
        if target_hit:
            exit_reason, exit_price, closed_ts = "target", target, timestamps[i]
            break

    if exit_reason is None:
        if not allow_eod_flat:
            return False  # incomplete session: no flattening at a mid-session price
        exit_reason, exit_price = "eod_flat", last_close  # closed_ts is the last bar

    risk = abs(entry - stop)
    realized_r = (exit_price - entry) / risk if is_long else (entry - exit_price) / risk

    closed_at = pd.Timestamp(closed_ts).to_pydatetime()
    trade.status = "closed"
    trade.exit_reason = exit_reason
    trade.exit_price = float(exit_price)
    trade.realized_r = float(realized_r)
    trade.closed_at = closed_at
    if trade.opened_at is not None:
        trade.hold_minutes = int((closed_at - trade.opened_at).total_seconds() // 60)
    return True
