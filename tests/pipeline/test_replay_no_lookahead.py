from pathlib import Path

import numpy as np
import pandas as pd

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_book

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amd_daily_2018.csv"


def _amd():
    df = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _key(t):
    return (t.ticker, t.variant, str(t.opened_date), round(t.entry_price or 0, 4),
            round(t.stop, 4), round(t.target, 4), t.exit_reason,
            None if t.realized_r is None else round(t.realized_r, 4))


def test_replay_book_is_stable_golden_master():
    book = sorted(_key(t) for t in replay_book({"AMD": _amd()}, timeframe="1d"))
    assert len(book) >= 1
    # Snapshot of the exact trade-level book over the AMD 2018 fixture. Any drift
    # (different fills, exits, or R) breaks this -- the load-bearing guarantee Tasks 3-4
    # build on (they perturb numbers; this proves the substrate).
    # Targets are anchored on the entry ceiling (the fill): each target sits exactly
    # min_target_r (1.5) R above the entry. RE-FROZEN 2026-07 when fill_slippage_atr's
    # default moved 0.0 -> 0.05 (the audit's net-of-cost fix): a target exit now fills at
    # target - 0.05*ATR, realizing ~1.4489R here; time_stop exits use the bar close (never
    # haircut) and are unchanged. Entry/stop/target levels are identical to the 0.0 book.
    # default + extguard_tight produce the same 2 continuation trades; cont_volband is too
    # selective to fire on this fixture (0 trades); rev_highvol only gates REVERSALS (none in
    # AMD 2018) so it mirrors default's continuation book. Reversal-only variants still book
    # their identical continuation fills -- harmless, the leaderboard filters by play_type.
    expected = [
        ("AMD", "default", "2018-07-05", 15.2354, 14.5719, 16.2306, "target", 1.4489),
        ("AMD", "default", "2018-07-06", 15.7311, 14.575, 17.4652, "time_stop", 0.6651),
        ("AMD", "extguard_tight", "2018-07-05", 15.2354, 14.5719, 16.2306, "target", 1.4489),
        ("AMD", "extguard_tight", "2018-07-06", 15.7311, 14.575, 17.4652, "time_stop", 0.6651),
        ("AMD", "rev_highvol", "2018-07-05", 15.2354, 14.5719, 16.2306, "target", 1.4489),
        ("AMD", "rev_highvol", "2018-07-06", 15.7311, 14.575, 17.4652, "time_stop", 0.6651),
    ]
    assert book == expected


# Bar j fires a continuation trade (a setup opens on this exact bar). It is the
# spike point AND the comparison cutoff, which is what gives the test teeth: the
# trade opened on bar j is screened off bar j-1 (its trigger), so spiking bar j
# onward must NOT change it. A 1-bar lookahead (trigger = bar j instead of j-1)
# would read the spiked bar and corrupt that trade -- exactly the bug we guard.
SPIKE_BAR = 338


def _synth(n=400):
    # A steady uptrend (slope) with shallow, frequent dips (low amplitude, short
    # period) so the sine troughs are pullbacks that stay above the slow EMA --
    # continuation setups, not deep-enough-to-be-reversal ones. Tuned so a
    # continuation trade fires on SPIKE_BAR (and several before it).
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = 50 + 0.15 * t + 1.2 * np.sin(t / 5.0)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.3
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=idx)


def test_future_bar_cannot_change_past_trades():
    # gate off so the synth fires setups; slippage off because this test isolates
    # TRIGGER/FILL lookahead -- with a haircut, a trade exiting AFTER the spike
    # legitimately depends on its (spiked) exit bar's ATR, which is not lookahead.
    cfg = StrategyConfig(max_extension_atr=0.0, fill_slippage_atr=0.0)
    full = _synth(400)
    spiked = full.copy()
    j = SPIKE_BAR
    spiked.iloc[j:, spiked.columns.get_loc("high")] *= 5.0
    spiked.iloc[j:, spiked.columns.get_loc("close")] *= 5.0
    # Compare every trade opened on or before the spike bar: under the real (no
    # lookahead) harness their trigger + fill are all <= bar j, so a spike from j
    # onward cannot move them. The trade opened exactly on bar j is the load-bearing
    # one -- its fill bar is spiked, but its trigger (bar j-1) is not.
    cutoff = full.index[j].date()

    def opened_by(frame):
        return {_key(t) for t in replay_book({"S": frame}, timeframe="1d",
                base_cfg=cfg, variants={"default": cfg})
                if t.opened_date is not None and t.opened_date <= cutoff}

    base, mutated = opened_by(full), opened_by(spiked)
    assert base, "expected pre-spike trades to compare"
    assert any(k[2] == str(cutoff) for k in base), "expected a trade opened ON the spike bar"
    assert base == mutated


def _spy(index, *, start=300.0, slope=0.5):
    # A SPY daily frame spanning the SAME date index as the ticker frame, trending up
    # so the last close stays above its 200-SMA -> trend resolves to "bull". Needs
    # >= 200 bars for classify_regime to leave the unknown branch.
    n = len(index)
    close = start + slope * np.arange(n)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=index)


def test_regime_stamped_only_when_spy_given():
    cfg = StrategyConfig(max_extension_atr=0.0)
    frame = _synth(400)
    spy = _spy(frame.index)
    # classify_regime needs >= 200 SPY bars; fills before SPY's 200th bar fail safe to an
    # unknown regime (correct, no-lookahead). Trades on/after that date carry a known tag.
    known_from = frame.index[199].date()

    # Default path: no SPY frame -> regime fields stay None on every trade (back-compat).
    plain = replay_book({"S": frame}, timeframe="1d", base_cfg=cfg,
                        variants={"default": cfg})
    assert plain, "expected trades to compare"
    assert all(t.market_trend is None and t.market_vol is None for t in plain)

    # Opted-in path: an uptrending SPY frame -> fills with >= 200 bars of history behind
    # them carry a known regime (NON-None trend AND vol).
    stamped = replay_book({"S": frame}, timeframe="1d", base_cfg=cfg,
                          variants={"default": cfg}, spy_daily=spy)
    late = [t for t in stamped if t.opened_date is not None and t.opened_date >= known_from]
    assert late, "expected trades opened with >= 200 SPY bars of history"
    assert all(t.market_trend is not None for t in late)
    assert all(t.market_vol is not None for t in late)
    # The uptrend should put SPY above its 200-SMA on every stamped fill.
    assert {t.market_trend for t in late} == {"bull"}


def test_regime_is_point_in_time_no_lookahead():
    cfg = StrategyConfig(max_extension_atr=0.0)
    frame = _synth(400)
    j = SPIKE_BAR
    cutoff = frame.index[j].date()

    # Two SPY frames identical up to and including bar j, but DIFFERENT after it: the
    # mutated copy collapses every post-j close far below the 200-SMA, which would flip
    # trend to "bear" if (and only if) a later bar leaked into an as-of-<=j classify.
    spy_base = _spy(frame.index)
    spy_mut = spy_base.copy()
    spy_mut.iloc[j + 1:, spy_mut.columns.get_loc("close")] = 1.0
    spy_mut.iloc[j + 1:, spy_mut.columns.get_loc("open")] = 1.0
    spy_mut.iloc[j + 1:, spy_mut.columns.get_loc("high")] = 1.5
    spy_mut.iloc[j + 1:, spy_mut.columns.get_loc("low")] = 0.5

    def regime_by_open(spy):
        out = {}
        for t in replay_book({"S": frame}, timeframe="1d", base_cfg=cfg,
                             variants={"default": cfg}, spy_daily=spy):
            if t.opened_date is not None and t.opened_date <= cutoff:
                out[_key(t)] = (t.market_trend, t.market_vol)
        return out

    base, mutated = regime_by_open(spy_base), regime_by_open(spy_mut)
    assert base, "expected trades opened on or before the cutoff"
    assert any(k[2] == str(cutoff) for k in base), "expected a trade opened ON the cutoff"
    # Every trade opened <= j sees only SPY bars <= its fill date, so mutating bars
    # AFTER j must not move any of their regime tags.
    assert base == mutated
