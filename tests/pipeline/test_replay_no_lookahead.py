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
    # Snapshot frozen on first green run: the exact trade-level book over the AMD 2018
    # fixture. Any drift (different fills, exits, or R) breaks this -- the load-bearing
    # guarantee Tasks 3-4 build on (they perturb numbers; this proves the substrate).
    expected = [
        ("AMD", "default", "2018-07-05", 15.2354, 14.5719, 15.76, "target", 0.7907),
        ("AMD", "default", "2018-07-06", 15.7311, 14.575, 16.3089, "target", 0.4998),
        ("AMD", "extguard_tight", "2018-07-05", 15.2354, 14.5719, 15.76, "target", 0.7907),
        ("AMD", "extguard_tight", "2018-07-06", 15.7311, 14.575, 16.3089, "target", 0.4998),
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
    cfg = StrategyConfig(max_extension_atr=0.0)   # gate off so the synth fires setups
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
