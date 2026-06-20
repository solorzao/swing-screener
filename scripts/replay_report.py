"""Offline replay report: an honest UPPER-BOUND screen of the live engine over history.

Fetches cached 1d bars for the (first N of the) universe, replays each ticker through
the same engine the nightly run uses (``replay_universe``), and prints a report:
overall expectancy, a per-play_type arm A/B (continuation vs reversal), and the
significance verdicts. ALL aggregation delegates to the already-tested pure functions in
``analytics.performance`` / ``analytics.significance`` -- this script is glue + I/O only.

D5 guardrail: replay numbers are survivorship-biased, optimistically-filled, and gross of
costs. They are an UPPER BOUND -- a screen a strategy must clear with margin, never a
tradeable edge. The closing banner says so loudly on purpose.

    python scripts/replay_report.py --max-tickers 20 --warmup 250

Fetches go through the on-disk cache (data.fetch); offline with a cold cache, every fetch
returns None and the report is empty -- that is expected, not an error.
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import PerformanceSummary, breakdown, summarize
from swing_screener.analytics.significance import evaluate_arms
from swing_screener.config import StrategyConfig
from swing_screener.dashboard.ui import format_arm_verdict
from swing_screener.data.fetch import fetch_bars
from swing_screener.data.universe import DEFAULT_SEED, load_universe
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.replay import replay_universe
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)

_PLAY_TYPES = ("continuation", "reversal")


def _fetch_bars_by_ticker(
    tickers: list[str], *, cache_dir: Path,
) -> dict[str, pd.DataFrame]:
    """Fetch 1d bars for each ticker, skipping None/empty frames (per-ticker isolation
    already lives in fetch_bars, which never raises). 2y of daily history per ticker."""
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = fetch_bars(ticker, "1d", cache_dir=cache_dir, period="2y")
        if df is not None and not df.empty:
            out[ticker] = df
        else:
            log.info("no bars for %s (None/empty); skipping", ticker)
    return out


def _print_summary(label: str, s: PerformanceSummary) -> None:
    """One overall line: counts plus the headline expectancy/PF."""
    print(
        f"{label:<14} n_total={s.n_total} n_filled={s.n_filled} "
        f"fill_rate={s.fill_rate:.2f} n_closed={s.n_closed} "
        f"win_rate={s.win_rate:.2f} expectancy_r={s.expectancy_r:+.3f} "
        f"profit_factor={s.profit_factor:.2f} avg_hold={s.avg_hold_bars:.1f}"
    )


def _print_arm_table(by_arm: dict[str, PerformanceSummary]) -> None:
    """Small per-arm table: arm, n_closed, win_rate, expectancy_r, profit_factor.

    Baseline first, then the rest alphabetically, so the control anchors the eye."""
    print(f"  {'arm':<18}{'n_closed':>9}{'win_rate':>10}{'expectancy_r':>14}{'profit_factor':>15}")
    ordered = sorted(by_arm, key=lambda a: (a != BASELINE, a))
    for arm in ordered:
        s = by_arm[arm]
        print(f"  {arm:<18}{s.n_closed:>9}{s.win_rate:>10.2f}"
              f"{s.expectancy_r:>+14.3f}{s.profit_factor:>15.2f}")


def _print_play_type_section(book: list[PaperTrade], play_type: str) -> None:
    """Per-play_type arm breakdown + significance verdicts.

    ``breakdown(..., "arm")`` groups the FULL closed set by arm correctly (every arm is
    its own group), so -- unlike the overall line -- there is no per-arm double counting
    to guard against here; the duplication is the point of the A/B."""
    closed = [t for t in book if t.play_type == play_type]
    print(f"\n== {play_type.upper()} ==")
    if not closed:
        print("  (no trades)")
        return

    _print_arm_table(breakdown(closed, "arm"))

    # Significance: compare every non-baseline arm to baseline (the multiple-comparisons
    # family). evaluate_arms takes challenger arm names; baseline is excluded.
    challengers = sorted({t.arm for t in closed if t.arm != BASELINE})
    if not challengers:
        return
    print("  verdicts:")
    verdicts = evaluate_arms(closed, challengers, baseline=BASELINE)
    for arm in challengers:
        print(f"    {format_arm_verdict(verdicts[arm])}")


def _print_guardrail() -> None:
    """D5: make it impossible to mistake a replay number for a tradeable edge."""
    bar = "!" * 78
    print(f"\n{bar}")
    print("INTERPRETATION GUARDRAIL (design decision D5)")
    print(bar)
    print(
        "Replay expectancy is a SURVIVORSHIP-BIASED, OPTIMISTICALLY-FILLED, "
        "GROSS-OF-COST\nUPPER BOUND. Fills assume exact stop/target prices with no "
        "slippage or fees, and\nthe universe is today's survivors. Treat these numbers "
        "as a SCREEN a strategy\nmust clear with margin -- NOT as proof of a tradeable "
        "edge."
    )
    print(bar)


def run_report(*, universe_path: Path, cache_dir: Path, max_tickers: int | None,
               warmup: int) -> None:
    """Fetch -> replay_universe -> print. Pure aggregation is delegated downstream."""
    entries = load_universe(universe_path)
    if max_tickers is not None:
        entries = entries[:max_tickers]
    tickers = [e.ticker for e in entries]
    log.info("replaying %d tickers (warmup=%d, cache_dir=%s)", len(tickers), warmup, cache_dir)

    bars_by_ticker = _fetch_bars_by_ticker(tickers, cache_dir=cache_dir)
    log.info("fetched bars for %d/%d tickers", len(bars_by_ticker), len(tickers))

    book = replay_universe(bars_by_ticker, StrategyConfig(), warmup_bars=warmup)

    print(f"\nReplay report -- {len(bars_by_ticker)} tickers, {len(book)} paper-trade rows "
          f"(all arms)\n")

    # OVERALL: the book carries one row PER ARM per fill, so summarizing the whole book
    # would triple-count every fill. Filter to the baseline arm for the headline counts,
    # mirroring run.py's "count distinct fills (one arm)" at run.py:299.
    baseline_book = [t for t in book if t.arm == BASELINE]
    _print_summary("baseline", summarize(baseline_book))

    # PER PLAY_TYPE: pass the full (multi-arm) closed set; breakdown groups by arm.
    for play_type in _PLAY_TYPES:
        _print_play_type_section(book, play_type)

    _print_guardrail()


def _force_utf8_stdout() -> None:
    """format_arm_verdict emits emoji (the dashboard renders them in a UTF-8 browser);
    a Windows console defaults to cp1252 and would UnicodeEncodeError on the first
    verdict. Reconfigure stdout to UTF-8 (replace on the off chance the stream can't).

    ``reconfigure`` only exists on a real ``TextIOWrapper`` (the console), not on every
    redirected stream, so look it up defensively; typeshed types ``sys.stdout`` as the
    abstract ``TextIO`` which doesn't declare it."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except ValueError:  # already-detached / unsupported stream
            pass


def main() -> None:
    _force_utf8_stdout()
    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="Offline replay report (upper-bound screen) over the universe.")
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="replay only the first N universe tickers (default: all)")
    parser.add_argument("--warmup", type=int, default=250,
                        help="warmup bars before the first fillable night (default: 250)")
    parser.add_argument("--universe", type=Path, default=DEFAULT_SEED,
                        help="universe CSV path (default: the packaged seed)")
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir,
                        help="bar cache dir (default: from load_settings())")
    # TODO(B6): --slippage-atr FLOAT -- apply an ATR-fraction haircut to every fill
    #   (slippage/cost model). Plugs in as a post-replay transform on `book` before the
    #   summaries, or threaded into the fill logic; keep it OFF by default so this report
    #   stays the pure optimistic upper bound. Not implemented here (separate task).
    # TODO(B7): --shuffle [SEED] -- placebo control that shuffles signal->bar associations
    #   to estimate the null expectancy. Plugs in via the `seed` already threaded through
    #   replay_universe/replay_ticker. Not implemented here (separate task).
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_report(universe_path=args.universe, cache_dir=args.cache_dir,
               max_tickers=args.max_tickers, warmup=args.warmup)


if __name__ == "__main__":
    main()
