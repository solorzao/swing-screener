"""Offline config-sweep optimizer -- the capstone of the measurement loop.

Drives the replay harness over a grid of screen configs, ranks them with the leaderboard's
significance, and proposes the next ``build_screen_variants`` set. A config grid is just a
variant set, so this reuses ``replay`` directly.

Walk-forward guard against overfitting: rank the grid on an IN-SAMPLE (earlier) slice of
history, then report the winner's OUT-OF-SAMPLE (later) performance. A config that only fits
the past wins in-sample but falls apart out-of-sample, which the report surfaces -- so a human
(or a later automated step) promotes only edges that actually hold up. Offline + deterministic;
no DB writes, no network beyond the cached parquet it reads.
"""

import argparse
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from swing_screener.analytics.performance import (
    PerformanceSummary,
    breakdown,
    leaderboard_order,
)
from swing_screener.config import StrategyConfig
from swing_screener.data.fetch import fetch_bars
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.proposed import QUEUED, ProposedVariant, to_config
from swing_screener.pipeline.replay import _warmup, format_leaderboard, replay, replay_book
from swing_screener.pipeline.variants import _assert_shared_indicators

log = logging.getLogger(__name__)

# The 1-D sweep: the freshness gate threshold (the knob this whole effort introduced). Includes
# the shipped default (2.0) so the incumbent is in the bake-off. Keep grids small + interpretable.
_EXT_GRID = (1.0, 1.5, 2.0, 2.5)

# Namespace for analyst-queued proposed-variant grid keys. The deterministic sweep points are
# named ``ext_<x>`` (see ``build_config_grid`` / ``propose._incumbent_name``); prefixing a
# proposed variant's key with this makes a collision with those -- or with the incumbent the
# propose() gate looks up by exact name -- STRUCTURALLY impossible, so a queued variant can
# never silently overwrite a deterministic/incumbent arm.
_PROPOSED_PREFIX = "proposed:"


@dataclass(frozen=True)
class OptimizeResult:
    in_sample: dict[str, PerformanceSummary]
    out_of_sample: dict[str, PerformanceSummary]
    winner: str | None   # best in-sample config that actually traded; None if none did
    # The raw OOS book grouped by variant -- the trade-level substrate propose()'s clustered
    # two-sample delta + placebo gates read. ``out_of_sample`` is the SAME book summarized, so
    # the two never disagree. Defaults empty so summary-only callers (format_report tests) need
    # not supply it; propose() treats a missing winner book as "cannot certify" (returns None).
    out_of_sample_trades: dict[str, list[PaperTrade]] = field(default_factory=dict)
    # Search-cost accounting (North Star #2, honest evidence): the number of configs the sweep
    # actually TESTED -- the full swept-grid size, INCLUDING analyst-queued variants -- NOT just
    # the configs that happened to trade (``len(in_sample)`` undercounts: a swept arm that drew
    # no trades is absent from ``in_sample`` yet still widened the search). ``propose()``'s
    # provenance reports this so a winner can't be cherry-picked from a silently-widened search.
    # 0 means "unknown" (a summary-only caller did not record the swept size); provenance then
    # falls back to the in-sample size.
    n_variants_tested: int = 0


def build_config_grid(
    base: StrategyConfig,
    proposed: list[ProposedVariant] | None = None,
) -> dict[str, StrategyConfig]:
    """Sweep ``max_extension_atr``; every grid point shares the base's indicator periods
    (replay reuses the base frames), enforced by the variants guard.

    Analyst-QUEUED ``ProposedVariant``s (Task 6) are MERGED in via ``to_config``: a
    ``status == QUEUED`` variant joins the swept grid; a non-queued one is skipped; and one
    whose delta fails ``to_config`` validation (a frozen-indicator or unknown-key delta) is
    skipped with a logged warning -- never poisoning the grid. Widening the search here is what
    the ``OptimizeResult.n_variants_tested`` search-cost accounting then pays for.

    Collision safety (honesty invariant): a proposed variant's grid key is NAMESPACED as
    ``f"{_PROPOSED_PREFIX}{pv.name}"`` so it can NEVER collide with -- and therefore never
    silently overwrite -- a deterministic ``ext_*`` sweep point (the baseline ``propose()``'s
    incumbent gate relies on by exact name). Among queued variants, a name clash (two mapping to
    the same namespaced key) keeps the FIRST and SKIPS the rest with a ``log.warning`` -- a
    duplicate analyst name is dropped, never silently collapsed. The net effect: ``len(grid)``
    (the source of ``n_variants_tested``) honestly counts every DISTINCT arm actually swept.
    """
    grid = {f"ext_{e:.1f}": replace(base, max_extension_atr=e) for e in _EXT_GRID}
    for name, cfg in grid.items():
        _assert_shared_indicators(base, name, cfg)
    for pv in proposed or []:
        if pv.status != QUEUED:
            continue
        key = f"{_PROPOSED_PREFIX}{pv.name}"
        if key in grid:
            log.warning(
                "skipping duplicate proposed variant %r: grid key %r already swept "
                "(keeping the first; a duplicate analyst name is dropped, not overwritten)",
                pv.name, key,
            )
            continue
        try:
            grid[key] = to_config(pv, base)
        except ValueError:
            log.warning(
                "skipping invalid proposed variant %r (delta %r): failed validation",
                pv.name, pv.delta, exc_info=True,
            )
    return grid


def _split(
    frames: Mapping[str, pd.DataFrame], oos_frac: float, lookback: int
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Split each frame into (in-sample, out-of-sample) by bar index. The OOS slice keeps
    ``lookback`` leading bars so detection has its warmup; its trades still come from the
    later period."""
    in_s: dict[str, pd.DataFrame] = {}
    out_s: dict[str, pd.DataFrame] = {}
    for ticker, f in frames.items():
        split = int(len(f) * (1.0 - oos_frac))
        in_s[ticker] = f.iloc[:split]
        out_s[ticker] = f.iloc[max(0, split - lookback):]
    return in_s, out_s


def optimize(
    frames: Mapping[str, pd.DataFrame],
    *,
    timeframe: str,
    base_cfg: StrategyConfig | None = None,
    grid: Mapping[str, StrategyConfig] | None = None,
    oos_frac: float = 0.3,
) -> OptimizeResult:
    """Sweep ``grid`` over ``frames`` with a walk-forward split and pick an in-sample winner.

    Ranks the grid on the in-sample slice (trust-tiered, like the leaderboard) and carries the
    winner's out-of-sample line so the report can show whether the edge holds up.
    """
    base_cfg = base_cfg or StrategyConfig()
    grid = grid or build_config_grid(base_cfg)
    in_frames, out_frames = _split(frames, oos_frac, _warmup(base_cfg))

    in_sample = replay(in_frames, timeframe=timeframe, base_cfg=base_cfg, variants=grid)

    # Build the OOS book ONCE and derive both views from it: the per-variant trade lists
    # (for propose()'s clustered delta + placebo gates) and the summarized leaderboard. Running
    # replay_book + breakdown here -- instead of replay() then a second replay_book() -- keeps the
    # expensive bar-by-bar walk single-pass and guarantees the trades and summaries agree.
    oos_book = replay_book(out_frames, timeframe=timeframe, base_cfg=base_cfg, variants=grid)
    out_of_sample_trades: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in oos_book:
        out_of_sample_trades[t.variant].append(t)
    out_of_sample = breakdown(oos_book, "variant")

    # Best in-sample config that actually traded (a 0-trade config can't be a winner).
    winner = next((n for n in leaderboard_order(in_sample) if in_sample[n].n_closed > 0), None)
    return OptimizeResult(
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        winner=winner,
        out_of_sample_trades=dict(out_of_sample_trades),
        # The honest search width: how many configs were SWEPT (incl. analyst-queued ones),
        # not how many traded. propose()'s provenance surfaces this.
        n_variants_tested=len(grid),
    )


def format_report(result: OptimizeResult) -> str:
    """Human-readable proposal: the in-sample leaderboard, the winner, and whether it holds
    out-of-sample (the promote / keep-current verdict)."""
    lines = ["IN-SAMPLE leaderboard:", format_leaderboard(result.in_sample), ""]
    if result.winner is None:
        lines.append("No config produced a closed trade in-sample; nothing to propose.")
        return "\n".join(lines)

    oos = result.out_of_sample.get(result.winner)
    lines.append(f"Winner (in-sample): {result.winner}")
    if oos is None or oos.n_closed == 0:
        lines.append("  out-of-sample: no closed trades -> cannot confirm; keep current config.")
    else:
        holds = oos.expectancy_ci_low > 0
        lines.append(
            f"  out-of-sample: expectancy_r={oos.expectancy_r:.2f} "
            f"(95% low {oos.expectancy_ci_low:.2f}), closed={oos.n_closed}"
        )
        lines.append(
            "  verdict: holds out-of-sample -> consider promoting into build_screen_variants"
            if holds else
            "  verdict: does NOT hold out-of-sample -> likely overfit, keep current config"
        )
    return "\n".join(lines)


def fetch_daily(tickers: list[str], cache_dir: Path) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per ticker via the cached fetch seam (reads the per-day parquet cache
    when present, else downloads). Tickers that fail to fetch are skipped (isolation)."""
    frames: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = fetch_bars(ticker, "1d", cache_dir=cache_dir)
        if df is not None and not df.empty:
            frames[ticker] = df
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep screen configs over daily history and propose a winner.")
    parser.add_argument("--tickers", required=True, help="comma-separated, e.g. AMD,NVDA")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--oos-frac", type=float, default=0.3,
                        help="fraction of each history held out for out-of-sample validation")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    frames = fetch_daily(tickers, args.cache_dir)
    if not frames:
        log.error("no data to optimize for %s (fetch failed?)", tickers)
        return
    print(format_report(optimize(frames, timeframe="1d", oos_frac=args.oos_frac)))  # noqa: T201


if __name__ == "__main__":
    main()
