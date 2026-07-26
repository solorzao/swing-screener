"""Option-chain snapshot seam + a warn-only liquidity guard.

The real yfinance fetch lives behind ``_fetch_chain_raw`` -- the single function
that touches the network -- exactly like ``data/fetch.py``'s ``_download`` seam, so
tests inject a fake and stay offline. Unlike the nightly equity fetch (per-ticker
isolation, returns None on failure), a chain snapshot is USER-triggered: on
persistent failure it RAISES ``RuntimeError`` so the failure is visible, never
silently swallowed.

No network at import time -- importing this module only imports yfinance; the
first live call happens inside ``_fetch_chain_raw``.
"""

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from swing_screener.options.config import GexConfig

# Chain frame column order shared with options/gex.py (expiry/strike/right/OI/iv).
_COLUMNS = ["expiry", "strike", "right", "open_interest", "iv"]

_EASTERN = ZoneInfo("America/New_York")


def _now_eastern() -> datetime:
    """Wall clock in US/Eastern as a NAIVE datetime (module-level so tests can
    freeze it). Naive to match the day-keyed lifecycle columns on GexSnapshot."""
    return datetime.now(tz=_EASTERN).replace(tzinfo=None)


@dataclass(frozen=True)
class ChainSnapshot:
    """One point-in-time options-chain pull for a single underlying."""

    underlying: str
    spot: float
    asof: datetime
    frame: pd.DataFrame


@dataclass(frozen=True)
class LiquidityReport:
    """Warn-only chain-liquidity verdict. ``thin`` never blocks analysis (any
    ticker is allowed); it just surfaces why the map may be unreliable."""

    thin: bool
    reasons: list[str]


def _side(df: pd.DataFrame, right: str, expiry: object) -> pd.DataFrame:
    """Normalize one side (calls or puts) of a yfinance option_chain frame."""
    return pd.DataFrame({
        "expiry": expiry,
        "strike": df["strike"].astype(float),
        "right": right,
        "open_interest": df["openInterest"].fillna(0).astype(int),
        "iv": df["impliedVolatility"].fillna(0.0).astype(float),
    })


def _spot_from_ticker(t: yf.Ticker) -> float:
    """Last price via fast_info, falling back to the last daily close.

    A non-finite fast_info price counts as missing: NaN is truthy, so it would
    otherwise sail past the None check and become the GEX map's spot."""
    spot = None
    try:
        spot = t.fast_info["lastPrice"]
    except (KeyError, TypeError):
        spot = None
    if spot is not None and not math.isfinite(float(spot)):
        spot = None
    if spot is None:
        spot = t.history(period="1d")["Close"].iloc[-1]
    return float(spot)


def _fetch_chain_raw(ticker: str, max_expiries: int, *, retries: int = 3,
                     backoff: float = 0.5, jitter: float = 0.5) -> tuple[float, pd.DataFrame]:
    """Real yfinance seam: spot + the nearest ``max_expiries`` expiries as one frame.

    Retries with exponential backoff plus random jitter (same shape as
    ``fetch_bars``). After exhaustion it RAISES ``RuntimeError`` -- chain snapshots
    are user-triggered, so a persistent failure must be visible, not a silent None.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            t = yf.Ticker(ticker)
            spot = _spot_from_ticker(t)
            frames: list[pd.DataFrame] = []
            for exp in t.options[:max_expiries]:
                oc = t.option_chain(exp)
                exp_date = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=UTC).date()
                frames.append(_side(oc.calls, "C", exp_date))
                frames.append(_side(oc.puts, "P", exp_date))
            if not frames:
                raise ValueError("no expiries returned")
            return float(spot), pd.concat(frames, ignore_index=True)
        except Exception as err:  # noqa: BLE001 -- network/parse failure: retry, then raise
            last_err = err
            if attempt < retries - 1:  # don't sleep after the final attempt
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, jitter))
    raise RuntimeError(f"chain snapshot failed for {ticker} after {retries} tries: {last_err}")


def snapshot_chain(ticker: str, *, cfg: GexConfig,
                   fetch: Callable[[str, int], tuple[float, pd.DataFrame]] = _fetch_chain_raw,
                   now: Callable[[], datetime] | None = None) -> ChainSnapshot:
    """Pull one chain snapshot through the ``fetch`` seam and normalize it.

    ``fetch`` defaults to the live yfinance seam; tests pass a fake. ``now``
    defaults to ``_now_eastern`` and is injectable so the timestamp can be frozen.
    """
    spot, frame = fetch(ticker, cfg.max_expiries)
    frame = frame[_COLUMNS].reset_index(drop=True)
    asof = (now or _now_eastern)()
    return ChainSnapshot(underlying=ticker, spot=float(spot), asof=asof, frame=frame)


def assess_liquidity(frame: pd.DataFrame, spot: float, cfg: GexConfig) -> LiquidityReport:
    """Warn-only liquidity check over the wall-detection window (spot +/- pct).

    Flags a thin chain when windowed total open interest or the count of
    OI-bearing strikes falls below the config floors. Never blocks -- the design's
    any-ticker rule keeps analysis available; this only annotates confidence.
    """
    reasons: list[str] = []
    lo, hi = spot * (1 - cfg.strike_window_pct), spot * (1 + cfg.strike_window_pct)
    windowed = frame[(frame["strike"] >= lo) & (frame["strike"] <= hi)]
    total_oi = int(windowed["open_interest"].sum()) if not windowed.empty else 0
    populated = int(windowed.loc[windowed["open_interest"] > 0, "strike"].nunique())
    if total_oi < cfg.min_total_oi:
        reasons.append(f"total OI {total_oi} < {cfg.min_total_oi}")
    if populated < cfg.min_populated_strikes:
        reasons.append(f"{populated} populated strikes < {cfg.min_populated_strikes}")
    return LiquidityReport(thin=bool(reasons), reasons=reasons)
