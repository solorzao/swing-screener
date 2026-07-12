"""Worst-case settlement of open options-lab paper trades from completed 5m bars.

The nightly job feeds the day's completed 5-minute bars per underlying; each open
lab trade is walked forward from its open time and closed at the first structural
touch. Conventions (pessimistic, matching the equity book):
  * A bar "touches" a level by high/low range inclusion.
  * Long: stop = low <= stop, target = high >= target. Short: mirrored.
  * Both levels inside one bar -> stop wins (we cannot see intrabar order, so we
    assume the adverse fill).
  * Neither level touched across the session -> flat at the last bar's close.
Stop/target fills land AT the level (structural, not an intrabar guess). Imported
robinhood trades never settle here -- their book is reconstructed from fills.
"""

import logging
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import OptionPaperTrade

log = logging.getLogger(__name__)


@dataclass
class SettleResult:
    settled: int
    skipped_no_bars: int


def settle_open_trades(
    session: Session, *, bars_by_underlying: dict[str, pd.DataFrame]
) -> SettleResult:
    """Close every open options-lab trade against its underlying's completed bars.

    Trades whose underlying has no frame are left open (retried on the next run).
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

        _close_trade(trade, relevant)
        settled += 1

    session.commit()
    return SettleResult(settled=settled, skipped_no_bars=skipped_no_bars)


def _close_trade(trade: OptionPaperTrade, bars: pd.DataFrame) -> None:
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
