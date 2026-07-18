import json
import logging
import math
import random
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

_COLS = ["open", "high", "low", "close", "volume"]

# The "daily bar is complete" invariant, enforced at the cache seam. The screen, the
# dashboard, exitcheck, and market_run all share the per-day parquet cache -- so a 2pm
# dashboard page-load used to pin TODAY'S IN-PROGRESS session bar as the day's truth, and
# the evening screen then detected, filled, and advanced off a half-formed candle (the
# 2026-07 audit's partial-bar contamination, previously guarded only by a docstring).
_EASTERN = ZoneInfo("America/New_York")
# TODO(2026-07-17 audit, H1): the fixed 16:00 close ignores half-days -- after a 1pm
# close this guard keeps dropping a COMPLETE final bar until 4pm. Doing this right
# needs a market calendar. Harmless-ish today: truncated frames are never cached
# (see fetch_bars), so a post-1pm-close fetch merely stays uncached for the day.
_MARKET_CLOSE_HOUR = 16  # 4pm ET
_LAST_5M_BAR = dt_time(15, 55)  # the session's final regular 5m bar starts 15:55 ET


def _now_eastern() -> datetime:
    """Wall clock in US/Eastern (module-level so tests can freeze it)."""
    return datetime.now(tz=_EASTERN)


def _drop_in_progress_daily_bar(df: pd.DataFrame, ticker: str) -> tuple[pd.DataFrame, bool]:
    """Drop the last row of a DAILY frame when it is today's not-yet-closed session bar.

    Yahoo serves the live in-progress bar during market hours. Rows for prior dates
    (or today's row fetched after the close) pass through untouched. Returns
    ``(frame, dropped)`` -- ``dropped`` tells ``fetch_bars`` the frame is truncated
    and must NOT be written to the per-day cache (a cached truncated frame ends
    YESTERDAY, so every later same-day consumer would miss today's bar entirely)."""
    if not len(df):
        return df, False
    now = _now_eastern()
    last_date = df.index[-1].date()
    if last_date == now.date() and now.hour < _MARKET_CLOSE_HOUR:
        log.info("dropping in-progress daily bar for %s (fetched %s ET, before the close)",
                 ticker, now.strftime("%H:%M"))
        return df.iloc[:-1], True
    return df, False


def _is_in_progress_5m_session(df: pd.DataFrame, ticker: str) -> bool:
    """True when a 5m frame's last bar sits on TODAY's session before the 15:55 ET
    close bar -- the session is still in progress. Such a frame is fine to RETURN
    but must never be cached: the day-keyed cache would pin the partial session,
    the post-close settle would cache-hit it and skip every untouched trade, and
    tomorrow's run would eod_flat them at TOMORROW's close (2026-07-17 audit,
    H1 follow-up -- the same poisoning as the daily in-progress bar, one interval
    over). Prior-day frames (e.g. a pre-market fetch) pass through untouched."""
    if not len(df):
        return False
    last = pd.Timestamp(df.index[-1])
    if last.tzinfo is not None:  # yfinance 5m bars are tz-aware; fixtures naive ET
        last = last.tz_convert(_EASTERN).tz_localize(None)
    now = _now_eastern()
    if last.date() == now.date() and last.time() < _LAST_5M_BAR:
        log.info("5m session in progress for %s (last bar %s ET); returning uncached",
                 ticker, last.strftime("%H:%M"))
        return True
    return False


def _cache_path(cache_dir: Path, interval: str, ticker: str, today: date) -> Path:
    """On-disk parquet cache location, keyed by (interval, ticker, day)."""
    return Path(cache_dir) / interval / f"{ticker}_{today:%Y%m%d}.parquet"


def _download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Thin, mockable wrapper around yfinance. Returns OHLCV with lowercase columns.

    Rows with any NaN OHLC are dropped (NaN volume kept -- index tickers)."""
    df = yf.download(ticker, interval=interval, period=period,
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    # One NaN close poisons the HA open recursion for every subsequent bar
    # (silently killing all detectors for the ticker) -- drop incomplete rows
    # here so every consumer is covered (the PR #104 class, fixed at the seam).
    # Volume is deliberately excluded: index tickers (^VIX) have no real volume.
    return df[_COLS].dropna(subset=["open", "high", "low", "close"])


def fetch_bars(ticker: str, interval: str, *, cache_dir: Path, period: str = "5y",
               today: date | None = None, retries: int = 3,
               backoff: float = 0.5, jitter: float = 0.5) -> pd.DataFrame | None:
    """Fetch OHLCV for one ticker, cached per (interval, ticker, day). Returns None
    on persistent failure (per-ticker isolation: never raises to the caller).

    ``period`` defaults to FIVE years because the 1mo frame resamples from the daily
    series and its detectors need ~56 monthly bars: 2y yielded ~24, so the monthly
    timeframe could never signal and the monthly digest was permanently empty
    (2026-07 audit). 5y yields ~60 -- above the floor. The cache key carries no
    period, so the horizon is a single default rather than a per-caller choice;
    intraday callers still pass their own short period (e.g. "60d" for 1h).

    Retries use exponential backoff plus random ``jitter`` so that, across the
    ~500-ticker universe, retries do not fire in lockstep -- a synchronized retry
    storm looks bot-like and worsens rate-limiting (a real risk from Azure
    datacenter IPs; see the deploy runbook's yfinance go/no-go gate)."""
    today = today or date.today()
    cache_file = _cache_path(cache_dir, interval, ticker, today)
    if cache_file.exists():
        try:
            return pd.read_parquet(cache_file)
        except Exception as err:  # corrupt/partial cache: fall through to a re-download
            log.warning("cache read failed for %s %s, re-fetching: %s", ticker, interval, err)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            df = _download(ticker, interval, period)
            if df is None or df.empty:
                raise ValueError("empty frame")
            skip_cache = False
            if interval == "1d":
                df, skip_cache = _drop_in_progress_daily_bar(df, ticker)
                if df.empty:
                    raise ValueError("empty frame after dropping the in-progress bar")
            elif interval == "5m":
                skip_cache = _is_in_progress_5m_session(df, ticker)
            if skip_cache:
                # Never cache an incomplete frame -- the cache-hit early return above
                # has no completeness check. A truncated 1d frame ends YESTERDAY (a
                # 2pm cockpit fetch would pin it and the evening screen would miss
                # today's triggers); an in-progress 5m frame pins a partial session
                # (2026-07-17 audit, H1). Uncached, the next call simply re-fetches.
                return df
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_file)
            return df
        except Exception as err:  # isolation is the whole point: never propagate
            last_err = err
            if attempt < retries - 1:  # don't sleep after the final attempt
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, jitter))
    log.warning("fetch failed for %s %s after %d tries: %s", ticker, interval, retries, last_err)
    return None


def avg_dollar_volume(frame: pd.DataFrame, window: int = 20) -> float | None:
    """Mean of close*volume over the last `window` bars; None if the frame is empty
    or the tail has no finite close*volume products (e.g. NaN volume, which the
    download seam deliberately keeps for index tickers). The skipna mean of an
    all-NaN tail is NaN, and apply_universe_metrics would write that straight to
    SQL Server, which rejects NaN floats (TDS 8023) -- missing reads as None,
    which apply_universe_metrics skips, preserving the prior value."""
    if frame is None or frame.empty:
        return None
    tail = frame.tail(window)
    value = float((tail["close"] * tail["volume"]).mean())
    return value if math.isfinite(value) else None


def _fast_info_market_cap(ticker: str) -> float | None:
    """Mockable wrapper: market cap via yfinance fast_info, or None if unavailable."""
    info = yf.Ticker(ticker).fast_info
    mc = None
    try:
        mc = info["market_cap"]  # FastInfo is mapping-like in modern yfinance
    except (KeyError, TypeError):
        mc = getattr(info, "market_cap", None)
    if mc is None:
        return None
    value = float(mc)
    # yfinance occasionally surfaces NaN/inf or a placeholder 0 for a missing cap;
    # treat any non-finite or non-positive value as "no market cap".
    return value if math.isfinite(value) and value > 0 else None


def fetch_market_cap(ticker: str, *, cache_dir: Path, today: date | None = None,
                     retries: int = 3, backoff: float = 0.5,
                     jitter: float = 0.5) -> float | None:
    """Market cap for one ticker, cached per (ticker, day). None on persistent failure or
    when fast_info has no market cap. Never raises (per-ticker isolation)."""
    today = today or date.today()
    cache_file = Path(cache_dir) / "marketcap" / f"{ticker}_{today:%Y%m%d}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())["market_cap"]  # type: ignore[no-any-return]
        except Exception as err:
            log.warning("market-cap cache read failed for %s: %s", ticker, err)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            mc = _fast_info_market_cap(ticker)
            if mc is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"market_cap": mc}))
            return mc  # a clean None (no market cap) is returned but not cached
        except Exception as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, jitter))
    log.warning("market-cap fetch failed for %s after %d tries: %s", ticker, retries, last_err)
    return None


def _info_sector(ticker: str) -> str | None:
    """Mockable wrapper: GICS sector via yfinance ``.info``, or None if unavailable."""
    info = yf.Ticker(ticker).info
    sector = info.get("sector") if isinstance(info, dict) else None
    if not sector:
        return None
    return str(sector).strip() or None


def fetch_sector(ticker: str, *, cache_dir: Path, today: date | None = None,
                 retries: int = 3, backoff: float = 0.5,
                 jitter: float = 0.5) -> str | None:
    """GICS sector for one ticker, cached per (ticker, day). None on persistent failure or
    when ``.info`` has no sector. Never raises (per-ticker isolation). Sector is sticky in
    the DB (apply_universe_metrics skips None), so a transient failure keeps the prior value."""
    today = today or date.today()
    cache_file = Path(cache_dir) / "sector" / f"{ticker}_{today:%Y%m%d}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())["sector"]  # type: ignore[no-any-return]
        except Exception as err:  # noqa: BLE001 -- a corrupt cache file must not abort the run
            log.warning("sector cache read failed for %s: %s", ticker, err)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            sector = _info_sector(ticker)
            if sector is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"sector": sector}))
            return sector  # a clean None (no sector) is returned but not cached
        except Exception as err:  # noqa: BLE001 -- per-ticker isolation, never raise
            last_err = err
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt) + random.uniform(0, jitter))
    log.warning("sector fetch failed for %s after %d tries: %s", ticker, retries, last_err)
    return None
