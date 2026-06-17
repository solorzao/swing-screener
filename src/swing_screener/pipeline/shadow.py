"""Shadow book: auto paper-trading of every screener signal.

Two entry points drive the shadow book across daily runs:

* ``open_from_signals`` resolves each candidate's fill against the bar that
  follows its trigger and records a ``PaperTrade`` (open for fills, terminal
  for missed/invalidated).
* ``advance_open`` walks every still-open paper trade forward one bar, exiting
  on a hard stop / momentum flip / target / time stop and otherwise bumping the
  running hold count.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.signals.entry_zone import EntryZone
from swing_screener.signals.exits import OpenTrade, evaluate_exit
from swing_screener.signals.fill import resolve_fill


@dataclass(frozen=True)
class FillCandidate:
    ticker: str
    timeframe: str
    horizon: str
    signal_score: float
    rank: int
    mtf_aligned: bool
    signal_id: int | None
    zone: EntryZone
    quality_tier: str = ""
    volatility_tier: str = ""
    oversold: bool = False
    play_type: str = "continuation"
    strength: str | None = None


def open_from_signals(
    session: Session,
    candidates: Sequence[FillCandidate],
    next_bars: Mapping[tuple[str, str], tuple[float, float]],
    *,
    fill_date: date,
) -> list[PaperTrade]:
    """Resolve each candidate against its next bar and persist a paper trade.

    Candidates without a next bar yet are skipped (resolved on a later run).
    Filled trades open at the worst-case in-zone price; missed/invalidated
    trades are recorded as terminal (``status="closed"``) and never advanced.
    """
    trades: list[PaperTrade] = []
    for cand in candidates:
        bar = next_bars.get((cand.ticker, cand.timeframe))
        if bar is None:
            continue
        bar_high, bar_low = bar
        fill = resolve_fill(cand.zone, bar_high, bar_low)

        common = {
            "ticker": cand.ticker,
            "timeframe": cand.timeframe,
            "horizon": cand.horizon,
            "play_type": cand.play_type,
            "strength": cand.strength,
            "signal_id": cand.signal_id,
            "signal_score": cand.signal_score,
            "rank": cand.rank,
            "mtf_aligned": cand.mtf_aligned,
            "quality_tier": cand.quality_tier,
            "volatility_tier": cand.volatility_tier,
            "oversold": cand.oversold,
            "fill_status": fill.status,
            "stop": cand.zone.stop,
            "target": cand.zone.target,
            "opened_date": fill_date,
        }

        risk = fill.price - cand.zone.stop if fill.price is not None else None
        if fill.status == "filled" and risk is not None and risk > 0:
            trade = PaperTrade(
                **common,
                entry_price=fill.price,
                entry_date=fill_date,
                risk=risk,
                status="open",
                hold_bars=0,
            )
        else:
            # missed/invalidated, OR a degenerate fill with non-positive risk that
            # we cannot honestly trade -> downgrade to invalidated, terminal, never
            # opened. This keeps every open trade's risk strictly positive so
            # advance_open's realized_r division can never divide by zero.
            common["fill_status"] = "invalidated" if fill.status == "filled" else fill.status
            trade = PaperTrade(
                **common,
                entry_price=None,
                entry_date=None,
                risk=cand.zone.risk,
                status="closed",
            )
        trades.append(trade)

    repo.save_paper_trades(session, trades)
    return trades


def advance_open(
    session: Session,
    latest_bars: Mapping[tuple[str, str], Mapping[str, float | bool]],
    cfg: StrategyConfig,
    *,
    today: date,
) -> None:
    """Advance every open paper trade by one bar, exiting or holding."""
    for pt in repo.load_open_paper_trades(session):
        if pt.entry_date == today or pt.last_advanced == today:
            continue
        bar = latest_bars.get((pt.ticker, pt.timeframe))
        if bar is None:
            continue

        # Open (filled) trades always carry a concrete entry and risk.
        assert pt.entry_price is not None and pt.risk is not None

        held = (pt.hold_bars or 0) + 1
        bar_high = float(bar["high"])
        # track highest high since fill (the trail reference used in Step D; harmless now)
        pt.high_water = max(
            pt.high_water if pt.high_water is not None else pt.entry_price, bar_high
        )

        partial_on = cfg.partial_frac > 0.0
        # Once partialed, the runner has NO fixed target (runs to stop/flip/time): suppress
        # the target branch by handing evaluate_exit an unreachable target.
        effective_target = float("inf") if pt.partial_done else pt.target
        trade = OpenTrade(
            entry=pt.entry_price,
            stop=pt.stop,
            target=effective_target,
            timeframe=pt.timeframe,
            bars_held=held,
        )
        decision = evaluate_exit(trade, bar, cfg)

        # PARTIAL scale-out: evaluate_exit ranks stop > momentum_flip > target, so a
        # "target" decision means the bar did NOT stop/flip this bar and the target was
        # hit. When the feature is on and we haven't partialed yet, convert that into a
        # scale-out (not a terminal exit): book the first leg, drop the stop to
        # breakeven, and let the runner run.
        if (
            partial_on
            and not pt.partial_done
            and decision.action == "EXIT"
            and decision.reason == "target"
        ):
            pt.partial_done = True
            pt.partial_price = pt.target
            pt.partial_r = (pt.target - pt.entry_price) / pt.risk
            pt.remaining_frac = 1.0 - cfg.partial_frac
            pt.stop = pt.entry_price            # breakeven after the partial
            pt.hold_bars = held
            pt.last_advanced = today
            continue

        if decision.action == "EXIT":
            # Exit-price modelling is deliberately asymmetric with entries: entries
            # are worst-cased (top of zone), but stop/target exits assume a clean
            # fill at the level (a gap-through bar fills worse in reality). So the
            # shadow book records stops/targets slightly favourably; momentum/time
            # exits use the bar close, which is realistic.
            if decision.reason == "stop":
                exit_price = pt.stop
            elif decision.reason == "target":      # only reachable when the feature is OFF
                exit_price = pt.target
            else:
                exit_price = float(bar["close"])

            final_r = (exit_price - pt.entry_price) / pt.risk
            # partial_r is always set alongside partial_done (the is-not-None check
            # both reflects that invariant and narrows the type for the multiply).
            partial_contrib = (
                cfg.partial_frac * pt.partial_r if pt.partial_r is not None else 0.0
            )
            pt.status = "closed"
            pt.exit_price = exit_price
            pt.exit_reason = decision.reason
            pt.exit_date = today
            pt.hold_bars = held
            pt.last_advanced = today
            pt.realized_r = partial_contrib + pt.remaining_frac * final_r

            repo.record_exit_event(
                session,
                is_paper=True,
                trade_id=pt.id,
                tier=decision.tier or "",
                reason=decision.reason or "",
                message=f"{pt.ticker} {pt.timeframe} {decision.reason} @ {exit_price}",
                created_date=today,
            )
        else:  # HOLD -> persist the running count
            pt.hold_bars = held
            pt.last_advanced = today

    session.commit()
