from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import Base
from swing_screener.pipeline.analyze import analyze_frames, analyze_reversals
from swing_screener.pipeline.arms import build_arms
from swing_screener.pipeline.replay import _higher_frames, replay_ticker
from swing_screener.pipeline.run import _bar_row
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.signals.frame import build_frame
from tests.pipeline._replay_fixtures import synthetic_daily


def test_one_replay_step_equals_a_direct_live_call():
    # DIFFERENTIAL guard on the replay's indexing contract (prior=:i, fill=i,
    # advance=:i+1). We build, by hand, the SAME single "live night" that run.py
    # would run over a slice -- detect on the prior bars, fill against bar i, advance
    # over :i+1 -- then assert replay_ticker reproduces the fills opened that day.
    # If replay's cursor arithmetic drifted (e.g. it advanced over the fill bar, or
    # detected one bar too far), entry/stop/target at open would diverge and trip
    # the comparison below.
    cfg = StrategyConfig()
    daily = synthetic_daily(400)
    enriched = build_frame(daily, cfg)
    # i=339 is a CONTINUATION fill date in this fixture (2025-04-18): a single 1d
    # signal fires there, so rank=1 under both the hand call (literal rank=1) and the
    # replay (which enumerates within play_type from 1 -- one signal => rank 1). Picked
    # so `direct`/`replayed` are non-empty and the comparison is meaningful.
    # To re-derive this pin if the fixture changes: run replay_ticker(..., warmup_bars=250)
    # and take any filled row's opened_date, mapped back to its cursor index, where
    # exactly one 1d signal fires that day.
    i = 339
    today = enriched.index[i].date()

    # --- direct "live night" over the same slice ---
    frames = {"1d": enriched.iloc[:i], **_higher_frames(daily.iloc[:i], cfg)}
    sigs = [r for r in analyze_frames("SYN", frames, cfg)
            + analyze_reversals("SYN", frames, cfg) if r.timeframe == "1d"]
    bar_i = enriched.iloc[i]
    next_bars = {("SYN", "1d"): (float(bar_i["high"]), float(bar_i["low"]))}
    cands = [FillCandidate(r.ticker, r.timeframe, r.horizon, r.score, 1, r.mtf_aligned,
                           None, r.zone, quality_tier=r.quality_tier,
                           volatility_tier=r.volatility_tier, oversold=r.oversold,
                           play_type=r.play_type, strength=r.strength) for r in sigs]
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        open_from_signals(s, cands, next_bars, fill_date=today, arms=tuple(build_arms(cfg)))
        advance_open(s, {("SYN", "1d"): _bar_row(enriched.iloc[:i + 1])},
                     build_arms(cfg), today=today)
        direct = {(t.ticker, t.arm, t.opened_date): (t.entry_price, t.stop, t.target)
                  for t in repo.load_all_paper_trades(s)}

    # Replay over [:i+1] must produce the SAME fills opened on `today`. warmup_bars
    # is i-2 (not i-1): replay_ticker early-returns when len(daily) <= warmup_bars+2,
    # and len here is i+1, so i-1 would skip the loop entirely (a vacuous run). i-2
    # makes the loop run cursors {i-2, i-1, i}, so cursor i is the final step -- the
    # exact step the direct call above models.
    book = replay_ticker("SYN", daily.iloc[:i + 1], cfg, warmup_bars=i - 2)
    replayed = {(t.ticker, t.arm, t.opened_date): (t.entry_price, t.stop, t.target)
                for t in book if t.opened_date == today}
    assert replayed, "test must compare at least one fill"
    # Full-dict equality (not replayed ⊆ direct): also catches a replay that UNDER- or
    # over-produces fills relative to the direct live-night call, not just value drift.
    assert replayed == direct
