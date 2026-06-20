"""Deterministic offline replay of the live engine over historical daily bars.

For one ticker, enrich the daily frame ONCE (every indicator is causal
ewm(adjust=False), so F.iloc[:i] is a valid point-in-time slice -- the same property
the live nightly run relies on at run.py:241). Then slide a cursor i and, at each
step, reproduce exactly one live "night":

    prior daily = enriched.iloc[:i]      # ends "yesterday" (bar i-1) -- the detection bar
    fill bar    = enriched.iloc[i]       # "today" -- the bar we fill the prior signal on
    advance bar = _bar_row(enriched.iloc[:i+1])

The engine functions (analyze_frames/analyze_reversals/open_from_signals/advance_open
/_bar_row) are REUSED verbatim against a throwaway in-memory SQLite -- never a twin.
1d-only (D1); higher timeframes are rebuilt per step only to reproduce mtf_aligned
faithfully (D2), then results are filtered to 1d. Output is a SCREEN, not proof (D5).
"""

import logging
from datetime import date

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.data.resample import resample_ohlcv
from swing_screener.db import repo
from swing_screener.db.models import Base, PaperTrade
from swing_screener.pipeline.analyze import analyze_frames, analyze_reversals
from swing_screener.pipeline.arms import build_arms
from swing_screener.pipeline.run import _bar_row
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.frame import build_frame

log = logging.getLogger(__name__)


def _higher_frames(prior_raw: pd.DataFrame, cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Resample the PRIOR daily slice to 1wk/1mo and enrich -- so the mtf_aligned read
    for a 1d signal matches what a live run on that date would have computed. Built per
    step because a once-resampled weekly frame's last (partial) bar would leak future
    daily bars.

    PERF: this per-step resample+enrich is the replay hot path (the daily frame is
    enriched once and sliced, but the higher frames cannot be -- see above -- so this
    is ~O(N^2) over a ticker's bars). Fine for single-ticker / few-hundred-bar runs;
    a future optimization (incremental higher-frame maintenance) is deferred per D2."""
    out: dict[str, pd.DataFrame] = {}
    wk = resample_ohlcv(prior_raw, "1W")
    mo = resample_ohlcv(prior_raw, "1ME")
    if len(wk):
        out["1wk"] = build_frame(wk, cfg)
    if len(mo):
        out["1mo"] = build_frame(mo, cfg)
    return out


def replay_ticker(
    ticker: str, daily_raw: pd.DataFrame, cfg: StrategyConfig, *,
    warmup_bars: int = 250, seed: int = 0,
) -> list[PaperTrade]:
    """Replay one ticker's 1d engine over daily_raw; return the resulting PaperTrade rows
    (detached from the throwaway session). Long-only, 1d-only.

    ``seed`` is accepted but currently unused: replay and the fixture are fully
    deterministic. It is a forward hook for the callers that DO randomize -- the B4
    ``replay_universe`` and the B7 shuffle-placebo control thread a seed through here."""
    if len(daily_raw) <= warmup_bars + 2:
        return []
    enriched = build_frame(daily_raw, cfg)          # enrich ONCE; slice thereafter
    arms = build_arms(cfg)
    arm_names = tuple(arms)

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        for i in range(warmup_bars, len(enriched)):
            today: date = enriched.index[i].date()
            prior_daily = enriched.iloc[:i]          # ends at bar i-1 ("yesterday")
            prior_raw = daily_raw.iloc[:i]
            frames = {"1d": prior_daily, **_higher_frames(prior_raw, cfg)}

            prior_signals = [
                r for r in (analyze_frames(ticker, frames, cfg)
                            + analyze_reversals(ticker, frames, cfg))
                if r.timeframe == "1d"               # D1: only fill 1d trades
            ]
            bar_i = enriched.iloc[i]
            next_bars = {(ticker, "1d"): (float(bar_i["high"]), float(bar_i["low"]))}
            cont = sorted((r for r in prior_signals if r.play_type == "continuation"),
                          key=lambda r: r.score, reverse=True)
            rev = sorted((r for r in prior_signals if r.play_type == "reversal"),
                         key=lambda r: r.score, reverse=True)
            candidates = [
                FillCandidate(r.ticker, r.timeframe, r.horizon, r.score, rank,
                              r.mtf_aligned, None, r.zone, quality_tier=r.quality_tier,
                              volatility_tier=r.volatility_tier, oversold=r.oversold,
                              play_type=r.play_type, strength=r.strength)
                for group in (cont, rev)
                for rank, r in enumerate(group, start=1)
            ]
            open_from_signals(s, candidates, next_bars, fill_date=today, arms=arm_names)
            latest_bars = {(ticker, "1d"): _bar_row(enriched.iloc[:i + 1])}
            advance_open(s, latest_bars, arms, today=today)

        return [_detach(t) for t in repo.load_all_paper_trades(s)]


def _detach(t: PaperTrade) -> PaperTrade:
    """Copy the row's columns into a fresh, session-free PaperTrade for aggregation.

    A plain copy (not ``expunge``/``make_transient``) is deliberate: callers replay
    many tickers into separate throwaway in-memory engines and concatenate the rows,
    so the result must be provably session- and identity-free. ``load_all_paper_trades``
    autoflushes first, so every column read here is flushed state."""
    cols = {c.name: getattr(t, c.name) for c in PaperTrade.__table__.columns
            if c.name != "id"}
    return PaperTrade(**cols)
