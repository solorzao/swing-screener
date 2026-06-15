import argparse
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.charts.render import render_chart
from swing_screener.config import StrategyConfig
from swing_screener.data.fetch import fetch_bars
from swing_screener.data.resample import resample_ohlcv
from swing_screener.data.universe import load_universe
from swing_screener.db import repo
from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.pipeline.analyze import SignalResult, analyze_frames, build_frames
from swing_screener.pipeline.shadow import FillCandidate, advance_open, open_from_signals
from swing_screener.storage.blob import blob_enabled, upload_chart

log = logging.getLogger(__name__)

_BAR_KEYS = ("low", "high", "close", "shaved_head", "bearish")


@dataclass(frozen=True)
class RunResult:
    n_signals: int
    n_paper_opened: int
    n_charts: int
    n_failed: int


def _fetch_all_timeframes(ticker: str, *, cache_dir: Path, today: date,
                          cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Fetch + resample the four timeframes for one ticker. Thin glue over the
    (already-tested) fetch + resample layers; mocked in tests."""
    out: dict[str, pd.DataFrame] = {}
    hourly = fetch_bars(ticker, "1h", cache_dir=cache_dir, today=today, period="60d")
    if hourly is not None and not hourly.empty:
        out["4h"] = resample_ohlcv(hourly, "4h")
    daily = fetch_bars(ticker, "1d", cache_dir=cache_dir, today=today)
    if daily is not None and not daily.empty:
        out["1d"] = daily
        out["1wk"] = resample_ohlcv(daily, "1W")
        out["1mo"] = resample_ohlcv(daily, "1ME")
    return out


def _bar_row(frame: pd.DataFrame) -> dict[str, float | bool]:
    last = frame.iloc[-1]
    return {k: last[k] for k in _BAR_KEYS}


def _to_signal(r: SignalResult, rank: int, run_date: date) -> Signal:
    return Signal(
        run_date=run_date, ticker=r.ticker, timeframe=r.timeframe, horizon=r.horizon,
        score=r.score, rank=rank, mtf_aligned=r.mtf_aligned, quality_tier=r.quality_tier,
        volatility_tier=r.volatility_tier, oversold=r.oversold, trigger_close=r.trigger_close,
        atr=r.atr, rsi=r.rsi, entry_floor=r.entry_floor, entry_ceiling=r.entry_ceiling,
        stop=r.stop, target=r.target,
    )


def run_screen(*, universe_path: Path, db_url: str, cache_dir: Path, chart_dir: Path,
               top_charts: int = 5, cfg: StrategyConfig | None = None,
               today: date | None = None, max_tickers: int | None = None) -> RunResult:
    cfg = cfg or StrategyConfig()
    today = today or date.today()
    universe = load_universe(universe_path)
    if max_tickers is not None:
        universe = universe[:max_tickers]
    engine = get_engine(db_url)

    today_results: list[SignalResult] = []
    prior: list[tuple[SignalResult, float, float]] = []  # (prior signal, next_high, next_low)
    latest_bars: dict[tuple[str, str], dict[str, float | bool]] = {}

    n_failed = 0
    for entry in universe:
        try:
            bars_by_tf = _fetch_all_timeframes(entry.ticker, cache_dir=cache_dir,
                                               today=today, cfg=cfg)
            if not bars_by_tf:
                continue
            frames = build_frames(bars_by_tf, cfg)
            for tf, f in frames.items():
                if len(f):
                    latest_bars[(entry.ticker, tf)] = _bar_row(f)
            today_results.extend(analyze_frames(entry.ticker, frames, cfg))
            prior_frames = {tf: f.iloc[:-1] for tf, f in frames.items() if len(f) > 1}
            for pr in analyze_frames(entry.ticker, prior_frames, cfg):
                last = frames[pr.timeframe].iloc[-1]
                prior.append((pr, float(last["high"]), float(last["low"])))
        except Exception:  # per-ticker isolation: one bad ticker never aborts the run
            log.warning("ticker %s failed; skipping", entry.ticker, exc_info=True)
            n_failed += 1
            continue

    today_results.sort(key=lambda r: r.score, reverse=True)
    prior.sort(key=lambda x: x[0].score, reverse=True)

    n_charts = 0
    n_paper_opened = 0
    with Session(engine) as s:
        repo.delete_signals_for(s, today)
        repo.delete_paper_trades_opened_on(s, today)
        signals = [_to_signal(r, rank, today) for rank, r in enumerate(today_results, start=1)]
        repo.save_signals(s, signals)

        for rank, r in enumerate(today_results[:top_charts], start=1):
            basename = f"{r.ticker}_{r.timeframe}_{today:%Y%m%d}.png"
            path = Path(chart_dir) / basename
            render_chart(r.frame, r.ctx, r.zone, path)
            if blob_enabled():
                # In Azure the filesystem is not shared across executions, so the
                # PNG lives in a private blob container. chart_path becomes the
                # blob KEY (what pdf/dashboard download back by), not a local path.
                key = f"{today:%Y%m%d}/{basename}"
                upload_chart(path, key)
                signals[rank - 1].chart_path = key
            else:
                signals[rank - 1].chart_path = str(path)
            n_charts += 1
        s.commit()

        # NOTE: `rank` here is the rank within the prior-bar (forward-tested) set
        # being filled this run -- a different ranking space from Signal.rank (which
        # ranks *today's* freshly published signals). The tags are denormalized onto
        # the paper trade so the shadow book is sliceable in QC without a join.
        candidates = [
            FillCandidate(pr.ticker, pr.timeframe, pr.horizon, pr.score, rank,
                          pr.mtf_aligned, None, pr.zone,
                          quality_tier=pr.quality_tier, volatility_tier=pr.volatility_tier,
                          oversold=pr.oversold)
            for rank, (pr, _h, _l) in enumerate(prior, start=1)
        ]
        next_bars = {(pr.ticker, pr.timeframe): (h, low) for (pr, h, low) in prior}
        opened = open_from_signals(s, candidates, next_bars, fill_date=today)
        n_paper_opened = sum(1 for t in opened if t.status == "open")
        advance_open(s, latest_bars, cfg, today=today)

    return RunResult(n_signals=len(today_results), n_paper_opened=n_paper_opened,
                     n_charts=n_charts, n_failed=n_failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the swing screener nightly pipeline.")
    # defaults match the dashboard + scripts/run_local.py so a bare pipeline run
    # and a bare dashboard launch point at the same DB/cache.
    parser.add_argument("--db", default="sqlite:///local.db")
    parser.add_argument("--universe", type=Path,
                        default=Path("src/swing_screener/data/universe_seed.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--chart-dir", type=Path, default=Path(".charts"))
    parser.add_argument("--top-charts", type=int, default=5)
    parser.add_argument("--max-tickers", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = run_screen(universe_path=args.universe, db_url=args.db, cache_dir=args.cache_dir,
                        chart_dir=args.chart_dir, top_charts=args.top_charts,
                        max_tickers=args.max_tickers)
    log.info("signals=%d paper_opened=%d charts=%d failed=%d",
             result.n_signals, result.n_paper_opened, result.n_charts, result.n_failed)


if __name__ == "__main__":
    main()
