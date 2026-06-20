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

Validity controls (all delegate to tested pure helpers):
  --slippage-atr FLOAT  apply an ATR-fraction haircut to LEVEL-based exits (a cost/
                        slippage model). 0.0 (default) is the pure optimistic upper
                        bound; run twice (0.0 and e.g. 0.05) to see optimistic vs
                        haircut expectancy. The header prints the value in force.
  --dev-until DATE      split the book into a development era (opened on/before DATE)
                        and a frozen confirmation era (after DATE), and print the
                        per-play_type A/B SEPARATELY for each. Any arm chosen on dev
                        must survive confirmation -- otherwise the choice is in-sample.
  --shuffle             ALSO print a placebo section: shuffle realized_r across the
                        closed book and re-run the A/B. Any 'winner' there means the
                        harness is leaking (an artifact, not signal). --shuffle-seed
                        sets the permutation seed.

Fetches go through the on-disk cache (data.fetch); offline with a cold cache, every fetch
returns None and the report is empty -- that is expected, not an error.
"""

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import PerformanceSummary, breakdown, summarize
from swing_screener.analytics.replay_validation import partition_by_era, shuffle_realized_r
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


def _print_book_body(book: list[PaperTrade]) -> None:
    """Overall baseline headline + the per-play_type arm A/B for one book.

    Shared by the combined report, each era of the --dev-until split, and the
    --shuffle placebo, so they all read identically and stay delegated to the same
    tested aggregations."""
    # OVERALL: the book carries one row PER ARM per fill, so summarizing the whole book
    # would triple-count every fill. Filter to the baseline arm for the headline counts,
    # mirroring run.py's "count distinct fills (one arm)" at run.py:299.
    baseline_book = [t for t in book if t.arm == BASELINE]
    _print_summary("baseline", summarize(baseline_book))

    # PER PLAY_TYPE: pass the full (multi-arm) closed set; breakdown groups by arm.
    for play_type in _PLAY_TYPES:
        _print_play_type_section(book, play_type)


def _print_era_split(book: list[PaperTrade], cutoff: date) -> None:
    """Print the per-play_type A/B SEPARATELY for the development era (opened on/before
    cutoff) and the frozen confirmation era (after it), with the out-of-sample warning.

    The split delegates to the tested ``partition_by_era``; both eras carry the full
    multi-arm set so the downstream breakdown/evaluate_arms see every arm."""
    split = partition_by_era(book, cutoff)
    print(f"\n=== DEV ERA (opened <= {cutoff.isoformat()}) "
          f"-- {len(split.dev)} rows ===")
    _print_book_body(split.dev)
    print(f"\n=== CONFIRMATION ERA (opened > {cutoff.isoformat()}) "
          f"-- {len(split.confirm)} rows ===")
    _print_book_body(split.confirm)
    print(
        "\nNOTE: any arm whose verdict is 'winner' on the DEV era must ALSO clear the "
        "bar on the\nCONFIRMATION era. An arm chosen on dev and not re-confirmed "
        "out-of-sample is in-sample\n-- a replay-derived decision validated on the data "
        "that produced it."
    )


def _print_placebo(book: list[PaperTrade], *, seed: int) -> None:
    """Negative control: shuffle realized_r across the closed book (seeded) and re-run
    the per-play_type A/B. A sound harness finds NO winner here; any 'winner' is an
    artifact of leakage, not signal. Additive -- the real report still stands."""
    shuffled = shuffle_realized_r(book, seed=seed)
    bar = "-" * 78
    print(f"\n{bar}")
    print(f"PLACEBO (label-shuffle, seed={seed}) -- this MUST show no winner")
    print(bar)
    _print_book_body(shuffled)
    print(
        "\nNOTE: realized_r was permuted across rows, destroying any real arm edge. A "
        "'winner'\nverdict in THIS section means the A/B harness is leaking -- the "
        "apparent edge in the\nreal report above would then be an artifact, not signal."
    )


def run_report(*, universe_path: Path, cache_dir: Path, max_tickers: int | None,
               warmup: int, slippage_atr: float, dev_until: date | None,
               shuffle: bool, shuffle_seed: int) -> None:
    """Fetch -> replay_universe -> print. Pure aggregation is delegated downstream."""
    entries = load_universe(universe_path)
    if max_tickers is not None:
        entries = entries[:max_tickers]
    tickers = [e.ticker for e in entries]
    log.info("replaying %d tickers (warmup=%d, cache_dir=%s)", len(tickers), warmup, cache_dir)

    bars_by_ticker = _fetch_bars_by_ticker(tickers, cache_dir=cache_dir)
    log.info("fetched bars for %d/%d tickers", len(bars_by_ticker), len(tickers))

    cfg = StrategyConfig(exit_slippage_atr=slippage_atr)
    book = replay_universe(bars_by_ticker, cfg, warmup_bars=warmup)

    haircut = ("OPTIMISTIC (exact-level fills, no cost)" if slippage_atr == 0.0
               else f"HAIRCUT ({slippage_atr:g} x ATR on level exits)")
    print(f"\nReplay report -- {len(bars_by_ticker)} tickers, {len(book)} paper-trade rows "
          f"(all arms)")
    print(f"slippage_atr={slippage_atr:g} -> fills are {haircut}\n")

    if dev_until is None:
        _print_book_body(book)
    else:
        _print_era_split(book, dev_until)

    if shuffle:
        _print_placebo(book, seed=shuffle_seed)

    _print_guardrail()


def _parse_date(s: str) -> date:
    """argparse type for --dev-until: parse a strict YYYY-MM-DD into a date, raising an
    ArgumentTypeError (not a bare ValueError) so a bad value gets a clean usage message."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from exc


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
    # B6: ATR-fraction haircut on level-based exits. 0.0 keeps the pure optimistic upper
    # bound; the knob lives in StrategyConfig.exit_slippage_atr and is threaded into the
    # replay so BOTH the live arms and replay de-bias together (config.py).
    parser.add_argument("--slippage-atr", type=float, default=0.0,
                        help="ATR-fraction haircut on stop/target fills "
                             "(0.0 = optimistic upper bound; e.g. 0.05 for a cost run)")
    # B5: out-of-sample era split. Any arm chosen on the dev era must re-confirm on the
    # frozen confirmation era (analytics.replay_validation.partition_by_era).
    parser.add_argument("--dev-until", type=_parse_date, default=None, metavar="YYYY-MM-DD",
                        help="split into dev (opened on/before this date) and "
                             "confirmation (after) eras and report each separately")
    # B7: label-shuffle placebo. Re-runs the A/B on a permuted closed book; any winner
    # there means the harness is leaking (analytics.replay_validation.shuffle_realized_r).
    parser.add_argument("--shuffle", action="store_true",
                        help="also print a placebo section (shuffled realized_r); a "
                             "'winner' there means the harness is leaking")
    parser.add_argument("--shuffle-seed", type=int, default=0,
                        help="seed for the --shuffle permutation (default: 0)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_report(universe_path=args.universe, cache_dir=args.cache_dir,
               max_tickers=args.max_tickers, warmup=args.warmup,
               slippage_atr=args.slippage_atr, dev_until=args.dev_until,
               shuffle=args.shuffle, shuffle_seed=args.shuffle_seed)


if __name__ == "__main__":
    main()
