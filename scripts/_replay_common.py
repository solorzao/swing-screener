"""Shared corpus loading for the scripts/replay_*.py experiment harnesses.

Every replay script used to carry a copy-pasted ``_unique_tickers`` + frame-loading
loop, and the copies had drifted: 9 scripts subtracted ``{"^VIX", "SPY"}`` from the
corpus and 9 did not. The drift was resolved deliberately (2026-07 audit, M1):

**The default is to exclude ^VIX and SPY, for every script.** Those two frames live
in the shared ``.cache/1d`` only as *market context* (SPY regime stamping, VIX-rank
gating) -- they were first fetched into the cache by the 2026-06-28 edge-discovery
campaign (67ae3d2), the same commit that introduced the exclusion in the scripts it
added. ``replay_book`` treats every corpus frame as a tradeable candidate, so leaving
them in simulates book trades on an untradeable volatility index (^VIX) and on the
benchmark ETF the strategy measures itself against (SPY).

Per-camp verdicts from the drift analysis:

- Excluding camp (fill_window, rotation_entry, queue_experiments, rev_confirm,
  rev_combo, spring, vix, sizing, rs): already correct; behavior unchanged. Scripts
  that need SPY/^VIX as context still load them separately via ``load_cached_daily``.
- Non-excluding camp, 2026-06-25/26 scripts (experiment, reversal_flip,
  reversal_strength, slippage_sweep, tournament, tournament2, tournament3): authored
  *before* SPY/^VIX ever existed in the cache, so their non-exclusion was a no-op at
  authoring time -- today's inclusion is environment drift, not intent. Flipped to
  the default exclusion.
- Non-excluding camp, 2026-06-28 scripts (regime, wave1): oversights in the very
  commit that added the exclusion to their siblings; regime even loads SPY separately
  as the regime benchmark while leaving it in the tradeable corpus. Flipped to the
  default exclusion.

A script that genuinely wants index/benchmark frames inside its corpus can pass
``exclude=()`` explicitly; none currently does.

Note the corpus is sorted, so with ``limit`` the exclusion also shifts which tickers
make the head slice (by at most two).
"""

from collections.abc import Collection, Sequence
from pathlib import Path

import pandas as pd

from swing_screener.pipeline.replay import load_cached_daily

DEFAULT_EXCLUDE: tuple[str, ...] = ("^VIX", "SPY")


def unique_tickers(
    cache_dir: Path, *, exclude: Collection[str] = DEFAULT_EXCLUDE
) -> list[str]:
    """Distinct tickers present as ``<cache>/1d/<TICKER>_<date>.parquet``, sorted.

    ``exclude`` (default ^VIX/SPY -- see module docstring) is subtracted first.
    """
    names = {p.name.split("_")[0] for p in (cache_dir / "1d").glob("*.parquet")}
    return sorted(names - set(exclude))


def load_replay_corpus(
    cache_dir: Path,
    *,
    exclude: Collection[str] = DEFAULT_EXCLUDE,
    min_bars: int = 60,
    limit: int = 0,
    as_of: str | None = None,
    tickers: Sequence[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Load the cached daily replay corpus: ticker -> OHLCV frame.

    Reproduces the loop every replay script used to copy-paste: list the distinct
    cached tickers (minus ``exclude``), optionally head-slice to ``limit`` (0 = all),
    then keep each frame with strictly more than ``min_bars`` rows (the historical
    ``len(df) > 60`` guard).

    Scripts that pre-process the ticker list themselves (e.g. ``--shard i/n`` slicing,
    or feeding the list to ``corpus_stamp``) pass it via ``tickers=``; ``exclude`` is
    ignored in that case. ``as_of`` pins cache vintage (see ``load_cached_daily``).
    """
    if tickers is None:
        tickers = unique_tickers(cache_dir, exclude=exclude)
    if limit:
        tickers = tickers[:limit]
    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        df = load_cached_daily(t, cache_dir, as_of)
        if df is not None and len(df) > min_bars:
            frames[t] = df
    return frames
