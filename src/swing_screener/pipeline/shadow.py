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
from datetime import date, datetime
from typing import cast

from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.arms import BASELINE
from swing_screener.signals.entry_zone import EntryZone
from swing_screener.signals.exits import OpenTrade, evaluate_exit
from swing_screener.signals.fill import resolve_fill


def _same_bucket(d1: date, d2: date, timeframe: str) -> bool:
    """True when two dates fall in the same bar bucket of ``timeframe`` -- the guard that
    keeps a trade from being advanced on the bar it filled in (next-bar semantics)."""
    if timeframe == "1wk":
        return d1.isocalendar()[:2] == d2.isocalendar()[:2]
    if timeframe == "1mo":
        return (d1.year, d1.month) == (d2.year, d2.month)
    return d1 == d2


def _is_softening(bar: Mapping[str, float | bool]) -> bool:
    """HA momentum is fading on this bar: a lower wick formed (no ``shaved_bottom``)
    OR the HA body shrank vs the prior bar (``body_shrinking``). The complement --
    ``shaved_bottom`` and not shrinking -- is a strong continuation that should hold
    the full position. Missing fields default to "softening" (permissive)."""
    shaved_bottom = bool(bar.get("shaved_bottom", False))
    body_shrinking = bool(bar.get("body_shrinking", False))
    return (not shaved_bottom) or body_shrinking


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
    conviction_tier: str = "base"
    # timestamp of the completed bar the signal triggered on. Weekly triggers are
    # re-detected on every daily run of the week, so this is the CROSS-RUN dedup key
    # (with ticker/timeframe/play_type/variant). None = no dedup (legacy/test callers).
    trigger_ts: datetime | None = None


def open_from_signals(
    session: Session,
    candidates: Sequence[FillCandidate],
    next_bars: Mapping[tuple[str, str], tuple[float, float]],
    *,
    fill_date: date,
    arms: Sequence[str] = (BASELINE,),
    variant: str = "default",
    market_trend: str | None = None,
    market_vol: str | None = None,
    vix_bucket: str | None = None,
) -> list[PaperTrade]:
    """Resolve each candidate against its next bar and persist a paper trade per arm.

    Candidates without a next bar yet are skipped (resolved on a later run).
    Filled trades open at the worst-case in-zone price; missed/invalidated
    trades are recorded as terminal (``status="closed"``) and never advanced.

    The fill economics are arm-independent (same entry/stop/target/risk), so each
    candidate is duplicated once per arm and tagged with ``arm``; the arms then
    diverge only in ``advance_open``. Both filled and terminal rows are duplicated
    so every arm is a complete book (per-arm ``fill_rate``/``n_total`` stay correct).

    ``variant`` tags the ENTRY/screen config these candidates came from (the orthogonal
    leaderboard dimension); the caller passes the screened fills for one variant at a time.

    CROSS-RUN DEDUP: a candidate whose ``trigger_ts`` is already booked for this variant
    (any arm -- arms are always written together) is skipped, so a weekly trigger that is
    re-detected on every daily run of its week books exactly once. ``trigger_ts=None``
    candidates are never deduped (legacy/test behavior).
    """
    keyed = [(c.ticker, c.timeframe, c.play_type, c.trigger_ts)
             for c in candidates if c.trigger_ts is not None]
    already = repo.booked_trigger_keys(session, variant=variant, keys=keyed)
    trades: list[PaperTrade] = []
    for cand in candidates:
        if (cand.trigger_ts is not None
                and (cand.ticker, cand.timeframe, cand.play_type, cand.trigger_ts) in already):
            continue
        bar = next_bars.get((cand.ticker, cand.timeframe))
        if bar is None:
            continue
        bar_high, bar_low = bar
        fill = resolve_fill(cand.zone, bar_high, bar_low)
        risk = fill.price - cand.zone.stop if fill.price is not None else None

        for arm in arms:
            common = {
                "ticker": cand.ticker,
                "timeframe": cand.timeframe,
                "horizon": cand.horizon,
                "play_type": cand.play_type,
                "strength": cand.strength,
                "conviction_tier": cand.conviction_tier,
                "signal_id": cand.signal_id,
                "signal_score": cand.signal_score,
                "rank": cand.rank,
                "mtf_aligned": cand.mtf_aligned,
                "quality_tier": cand.quality_tier,
                "volatility_tier": cand.volatility_tier,
                "oversold": cand.oversold,
                "arm": arm,
                "variant": variant,
                "trigger_ts": cand.trigger_ts,
                "market_trend": market_trend,
                "market_vol": market_vol,
                "vix_bucket": vix_bucket,
                "fill_status": fill.status,
                "stop": cand.zone.stop,
                "target": cand.zone.target,
                "opened_date": fill_date,
            }

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
                common["fill_status"] = (
                    "invalidated" if fill.status == "filled" else fill.status
                )
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
    latest_bars: Mapping[tuple[str, str], Mapping[str, float | bool | date]],
    arms: StrategyConfig | Mapping[str, StrategyConfig],
    *,
    today: date,
) -> None:
    """Advance every open paper trade by one COMPLETED bar of its own timeframe.

    ``arms`` is either a single config (back-compat: treated as the ``baseline``
    arm) or a ``{arm_name: config}`` mapping; each open trade is advanced under its
    own arm's config so the parallel arms diverge only in exit management.

    Each bar may carry a ``bar_date`` (the completed bar's label, supplied by the
    screen's ``_latest_completed_bar``); a bar without one is treated as today's
    (the pre-existing daily semantics). A trade advances at most once per DISTINCT
    bar label -- previously 1wk trades advanced once per DAILY run against the
    partial weekly bucket, so the 8-weekly-bar time stop fired after 8 trading
    days and momentum flips were read off half-formed weekly candles (2026-07
    audit). ``last_advanced`` therefore stores the bar label, not the run date.
    """
    arm_cfgs = {BASELINE: arms} if isinstance(arms, StrategyConfig) else dict(arms)
    # exclude_live: the bar-stepper must NEVER advance an account="live" row -- a live
    # position is filled/closed by the BROKER and owned by reconcile_live, the disjoint
    # engine. Stepping one would invent a simulated fill over broker reality.
    for pt in repo.load_open_paper_trades(session, exclude_live=True):
        bar = latest_bars.get((pt.ticker, pt.timeframe))
        if bar is None:
            continue
        raw_bar_date = bar.get("bar_date")
        bar_date = raw_bar_date if isinstance(raw_bar_date, date) else today
        # Aside from "bar_date" every value is numeric/bool (the exit machinery's
        # contract; evaluate_exit/_is_softening never read "bar_date"). The cast
        # records that narrowing for the calls below without copying the mapping.
        num_bar = cast("Mapping[str, float | bool]", bar)
        # Never advance on the bucket the trade filled in (next-bar semantics), and
        # never re-advance a bar already counted (idempotent per bar label).
        if pt.entry_date is not None and _same_bucket(pt.entry_date, bar_date, pt.timeframe):
            continue
        if pt.last_advanced is not None and bar_date <= pt.last_advanced:
            continue
        cfg = arm_cfgs.get(pt.arm)
        if cfg is None:
            continue  # arm dropped from the roster -> leave the trade open, don't guess

        # Open (filled) trades always carry a concrete entry and risk.
        assert pt.entry_price is not None and pt.risk is not None

        held = (pt.hold_bars or 0) + 1
        bar_high = float(num_bar["high"])
        # high_water is the highest high since the fill. Read the PRIOR bar's value
        # first: the Chandelier trail places this bar's stop off it, so we never use
        # this bar's own high to decide whether this bar stops out (no intra-bar
        # lookahead). It's folded forward to include this bar's high afterwards.
        prior_high_water = pt.high_water if pt.high_water is not None else pt.entry_price

        # Post-partial runner trail (Step D). Ratchet the stop up to high_water - m*ATR,
        # never down (max with the current stop) and never below breakeven (the stop
        # starts there at the partial, so the ratchet preserves it). Pre-partial trades
        # keep their hard stop; a non-positive/NaN ATR (undefined early bars) skips the
        # trail this bar rather than poisoning the stop.
        atr_val = float(num_bar.get("atr", 0.0))
        # Fill-pessimism haircut on LEVEL fills (worse for a long). 0 when off or atr undefined.
        slip = (
            cfg.fill_slippage_atr * atr_val
            if (cfg.fill_slippage_atr > 0.0 and atr_val > 0.0)
            else 0.0
        )
        if pt.partial_done and cfg.trail_mode == "chandelier" and atr_val > 0.0:
            pt.stop = max(pt.stop, prior_high_water - cfg.chandelier_atr_mult * atr_val)

        pt.high_water = max(prior_high_water, bar_high)

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
            play_type=pt.play_type,
        )
        decision = evaluate_exit(trade, num_bar, cfg)

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
            # Conditional gate: when the arm requires softening, a STRONG target-touch
            # is not scaled -- suppress the target exit and HOLD the full position so the
            # winner can run. It's re-evaluated every bar, so it partials the moment
            # momentum softens while still at/above the target. (A strong touch that is
            # also past the time stop keeps riding -- you don't time-stop a breakout to
            # new ground; rare, deliberate.)
            if cfg.partial_require_softening and not _is_softening(num_bar):
                pt.hold_bars = held
                pt.last_advanced = bar_date
                continue
            pt.partial_done = True
            pt.partial_price = pt.target - slip
            pt.partial_r = (pt.partial_price - pt.entry_price) / pt.risk
            pt.remaining_frac = 1.0 - cfg.partial_frac
            pt.stop = pt.entry_price            # breakeven after the partial
            pt.hold_bars = held
            pt.last_advanced = bar_date
            continue

        if decision.action == "EXIT":
            # Exit-price modelling is deliberately asymmetric with entries: entries
            # are worst-cased (top of zone), while stop/target exits fill at the level
            # MINUS the optional `slip` haircut (subtracted because the book is long-only,
            # so a worse fill is always a lower price). With `fill_slippage_atr == 0.0`
            # (the default) slip is 0, so stops/targets record at the exact level
            # (slightly favourable, since a gap-through fills worse in reality); a
            # non-zero haircut pessimises them by `slip`. momentum/time exits use the bar
            # close, which is realistic, and are never haircut.
            if decision.reason == "stop":
                exit_price = pt.stop - slip
            elif decision.reason == "target":      # only an all-or-nothing arm (partial_frac == 0)
                exit_price = pt.target - slip
            else:
                exit_price = float(num_bar["close"])

            final_r = (exit_price - pt.entry_price) / pt.risk
            # Size-weight realized R off PERSISTED state, never the live config. The
            # booked partial leg carried (1 - remaining_frac) of the size at the moment
            # it filled; the runner carries the rest. Deriving the partial weight from
            # remaining_frac (not cfg.partial_frac) keeps the two legs summing to 1.0
            # even if partial_frac is retuned mid-flight. partial_done is the single
            # source of truth -- partial_r is set in lockstep with it, so the assert
            # both documents that invariant and narrows the type for the multiply.
            if pt.partial_done:
                assert pt.partial_r is not None
                partial_contrib = (1.0 - pt.remaining_frac) * pt.partial_r
            else:
                partial_contrib = 0.0
            pt.status = "closed"
            pt.exit_price = exit_price
            pt.exit_reason = decision.reason
            pt.exit_date = today
            pt.hold_bars = held
            pt.last_advanced = bar_date
            pt.realized_r = partial_contrib + pt.remaining_frac * final_r

            repo.record_exit_event(
                session,
                is_paper=True,
                account=pt.account,
                trade_id=pt.id,
                tier=decision.tier or "",
                reason=decision.reason or "",
                message=f"{pt.ticker} {pt.timeframe} {decision.reason} @ {exit_price}",
                created_date=today,
            )
        else:  # HOLD -> persist the running count
            pt.hold_bars = held
            pt.last_advanced = bar_date

    session.commit()
