"""Auto-proposal: turn an optimizer sweep into a reviewable config-change PR.

The scheduled half of the build -> measure -> optimize -> repeat loop, WITHOUT removing
the human. It runs the walk-forward optimizer, and only when a swept config beats the
incumbent on a *trusted, out-of-sample* basis does it propose changing the live
``max_extension_atr`` default -- by editing ``config.py`` and emitting a PR title/body for a
GitHub Actions workflow to open. A human still reviews and merges the PR; nothing
auto-deploys. Conservative by design: a thin or in-sample-only edge proposes nothing, so the
loop never spams overfit changes.
"""

import argparse
import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from swing_screener.analytics.performance import (
    MIN_LEADERBOARD_N,
    _CLUSTER_FLOOR,
    _is_closed_filled,
    clustered_two_sample_delta_low,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.db.models import PaperTrade
from swing_screener.pipeline.optimize import (
    _PROPOSED_PREFIX,
    OptimizeResult,
    build_config_grid,
    fetch_daily,
    grid_scopes,
    optimize,
    scoped_trades,
)
from swing_screener.pipeline.proposed import load_proposed_for
from swing_screener.pipeline.replay import format_leaderboard
from swing_screener.settings import resolve_edge_dir

log = logging.getLogger(__name__)

# Default source of the live gate default (relative to the repo root the job checks out).
_CONFIG_PATH = Path("src/swing_screener/config.py")
_GATE_RE = re.compile(r"(max_extension_atr:\s*float\s*=\s*)[0-9.]+")


@dataclass(frozen=True)
class Proposal:
    knob: str           # the StrategyConfig field to change (currently always the gate)
    current: float
    proposed: float
    title: str
    body: str


def _incumbent_name(base_cfg: StrategyConfig) -> str:
    """The grid name for the currently-shipped gate (matches build_config_grid's format)."""
    return f"ext_{base_cfg.max_extension_atr:.1f}"


def _closed_by_ticker(trades: list[PaperTrade]) -> dict[str, list[float]]:
    """Realized R per ticker over closed-filled trades (the bootstrap's clusters)."""
    d: dict[str, list[float]] = {}
    for t in trades:
        # The second clause is redundant at runtime (_is_closed_filled already requires it)
        # but narrows realized_r from float | None to float for mypy.
        if _is_closed_filled(t) and t.realized_r is not None:
            d.setdefault(t.ticker, []).append(t.realized_r)
    return d


def _clustered_two_sample_delta_low(
    winner: list[PaperTrade], incumbent: list[PaperTrade],
) -> float:
    """Lower 2.5% bound on (mean winner R - mean incumbent R), resampling TICKERS with
    replacement INDEPENDENTLY in each book (two-sample clustered bootstrap, NOT paired --
    variants are not same-sample, D1). Delegates to the shared
    ``clustered_two_sample_delta_low`` primitive over each book's per-ticker R."""
    return clustered_two_sample_delta_low(
        _closed_by_ticker(winner), _closed_by_ticker(incumbent)
    )


def _placebo_cleared(
    winner: list[PaperTrade], incumbent: list[PaperTrade], observed_delta: float,
    *, seed: int = 12345, n_shuffle: int = 1000,
) -> bool:
    """Pool both books' R, randomly relabel winner/incumbent (preserving sizes), and
    confirm the observed delta exceeds the 95th percentile of the shuffled null. If a
    random relabel reproduces the edge, it is an artifact, not signal."""
    wv = [t.realized_r for t in winner if _is_closed_filled(t) and t.realized_r is not None]
    iv = [t.realized_r for t in incumbent if _is_closed_filled(t) and t.realized_r is not None]
    nw = len(wv)
    pool = np.array(wv + iv, dtype=float)
    if nw == 0 or nw == len(pool):
        return False
    rng = np.random.default_rng(seed)
    null = np.empty(n_shuffle)
    for b in range(n_shuffle):
        perm = rng.permutation(pool)
        null[b] = perm[:nw].mean() - perm[nw:].mean()
    return observed_delta > float(np.percentile(null, 95))


def _provenance(result: OptimizeResult) -> str:
    """A reproducibility footer for the PR body: a stable hash of the swept grid, the current
    git SHA, and the search-cost counts -- so the researcher degrees-of-freedom behind a
    proposal are auditable after the fact.

    ``n_variants_tested`` is the SWEPT-grid size (the true search width, INCLUDING any
    analyst-queued variants) -- the honest multiple-comparisons denominator. It is reported
    distinctly from ``n_configs`` (the count that actually traded in-sample): a widened search
    is visible even when the extra arms drew no trades. When a summary-only caller did not
    record the swept size (``n_variants_tested == 0``), it falls back to the in-sample size so
    the footer is never silently empty."""
    grid_hash = hashlib.sha1(",".join(sorted(result.in_sample)).encode()).hexdigest()[:12]
    n_tested = result.n_variants_tested or len(result.in_sample)
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True, text=True, check=True,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        sha = "unknown"
    return (f"\n### Provenance\n"
            f"grid: `{grid_hash}` · sha: `{sha}` · n_configs={len(result.in_sample)} · "
            f"n_variants_tested={n_tested}\n")


def propose(
    result: OptimizeResult,
    base_cfg: StrategyConfig,
    *,
    min_oos_trades: int = MIN_LEADERBOARD_N,
) -> Proposal | None:
    """Propose a gate change iff a swept config genuinely beats the incumbent.

    Guards (all must hold, or nothing is proposed):
      * there is an in-sample winner, and it is NOT already the incumbent;
      * the winner has a TRUSTED out-of-sample sample (``n_closed >= min_oos_trades``);
      * its out-of-sample lower 95% bound is positive (a real edge, not noise);
      * its out-of-sample expectancy beats the incumbent's (point estimate);
      * the winner's OOS book spans ``>= _CLUSTER_FLOOR`` distinct tickers (enough clusters
        to certify the difference, not a one-name artifact);
      * the clustered TWO-SAMPLE delta (winner minus incumbent, resampling tickers
        independently in each book -- D1: NOT paired) has a lower 2.5% bound above 0; and
      * a label-shuffle PLACEBO is cleared (a random relabel of the pooled R does not
        reproduce the observed edge -- so the edge is signal, not luck).
    The out-of-sample gates are the anti-overfit teeth: an edge that only shows up
    in-sample, on too few names, or that a coin-flip relabel reproduces, never makes it
    into a PR.
    """
    incumbent_name = _incumbent_name(base_cfg)
    winner = result.winner
    if winner is None or winner == incumbent_name:
        return None

    w_oos = result.out_of_sample.get(winner)
    if w_oos is None or w_oos.n_closed < min_oos_trades or w_oos.expectancy_ci_low <= 0:
        return None

    # Every comparison below runs on the WINNER'S SCOPE (the play type its knob touches;
    # see optimize.grid_scopes): the pooled book's untouched-play-type fills are byte-
    # identical across arms, so leaving them in dilutes a real single-play-type delta
    # toward zero and the gates below become structurally near-blind (2026-07 review).
    # The winner's own summary line is already scope-sliced by optimize(); the incumbent's
    # line is recomputed here on the same scope so the comparison is apples-to-apples.
    winner_trades = scoped_trades(
        result.out_of_sample_trades.get(winner, []), winner, result.scopes)
    incumbent_trades = scoped_trades(
        result.out_of_sample_trades.get(incumbent_name, []), winner, result.scopes)

    inc_scoped = summarize(incumbent_trades) if incumbent_trades else None
    inc_oos_expectancy = (
        inc_scoped.expectancy_r if inc_scoped is not None and inc_scoped.n_closed > 0 else 0.0
    )
    if w_oos.expectancy_r <= inc_oos_expectancy:
        return None

    # Trade-level teeth (D1: a TWO-SAMPLE comparison of independent books, clustered by ticker --
    # the variants produce different fills, so this is NOT the paired arm A/B). The summary gates
    # above only test the winner's own line; these test the winner-vs-incumbent DIFFERENCE.
    if len(_closed_by_ticker(winner_trades)) < _CLUSTER_FLOOR:
        return None
    if _clustered_two_sample_delta_low(winner_trades, incumbent_trades) <= 0:
        return None
    # Note the deliberate weighting split: the clustered delta gate above is TICKER-weighted
    # (resamples whole tickers, respecting correlation), while observed_delta + the placebo
    # below are TRADE-weighted (pooled per-trade R). Both must pass; on a borderline case they
    # can disagree, which is intended -- the clustered gate is the correlation-aware one.
    observed_delta = w_oos.expectancy_r - inc_oos_expectancy
    if not _placebo_cleared(winner_trades, incumbent_trades, observed_delta):
        return None

    if winner.startswith(_PROPOSED_PREFIX):
        # An analyst-QUEUED variant won the sweep AND cleared every promotion gate.
        # Auto-editing config is gate-only by design (apply_to_config rewrites one knob;
        # an arbitrary analyst delta stays a HUMAN promotion, per North Star #1/#3) --
        # so surface the win loudly instead of crashing on the name parse or, worse,
        # passing silently.
        log.warning(
            "analyst-queued variant %r beat the incumbent OUT-OF-SAMPLE and cleared every "
            "promotion gate (expectancy %.2fR, 95%% low %.2f, n=%d) -- promotion is a HUMAN "
            "act: review its delta in edge/*.proposed.json and apply it by hand.",
            winner, w_oos.expectancy_r, w_oos.expectancy_ci_low, w_oos.n_closed)
        return None

    proposed = float(winner.removeprefix("ext_"))
    title = (f"optimizer: propose max_extension_atr "
             f"{base_cfg.max_extension_atr} → {proposed}")
    body = (
        "Auto-generated by the scheduled optimizer (`pipeline.propose`). **Review before "
        "merging** — nothing is applied without your approval.\n\n"
        f"A walk-forward sweep finds `max_extension_atr={proposed}` beats the incumbent "
        f"`{base_cfg.max_extension_atr}` on a held-out, out-of-sample basis "
        f"(expectancy {w_oos.expectancy_r:.2f}R, 95% low {w_oos.expectancy_ci_low:.2f}, "
        f"n={w_oos.n_closed}).\n\n"
        "### In-sample leaderboard\n```\n" + format_leaderboard(result.in_sample) + "\n```\n"
        "### Out-of-sample leaderboard\n```\n" + format_leaderboard(result.out_of_sample) + "\n```\n"
        + _provenance(result)
    )
    return Proposal(knob="max_extension_atr", current=base_cfg.max_extension_atr,
                    proposed=proposed, title=title, body=body)


def apply_to_config(source: str, proposal: Proposal) -> str:
    """Return ``config.py`` source with the gate default set to ``proposal.proposed``.

    Replaces only the ``max_extension_atr`` default literal (first match); raises if the
    field isn't found, so a refactor that moved/renamed it fails loudly instead of silently
    producing an empty diff.
    """
    new, n = _GATE_RE.subn(rf"\g<1>{proposal.proposed}", source, count=1)
    if n == 0:
        raise ValueError("max_extension_atr default not found in config source")
    return new


def _grid_with_queued(
    base_cfg: StrategyConfig, edge_dir: Path
) -> tuple[dict[str, StrategyConfig], dict[str, str]]:
    """The auto-propose grid + its play-type scopes: the standard gate sweep PLUS every
    analyst-QUEUED variant from ``edge/*.proposed.json`` (both play types -- the replay
    books both, and every leaderboard/gate line is sliced to the play type each knob
    touches via the returned scopes). This is the reflection-to-optimizer handoff the
    queue exists for: reflection drafts candidates "for the optimizer to sweep", and
    until this was wired the scheduled sweep never loaded them (2026-07 audit).
    ``to_config`` validates every delta inside ``build_config_grid``; an invalid one is
    skipped with a warning, never poisoning the grid.
    """
    queued = [pv for pt in ("continuation", "reversal")
              for pv in load_proposed_for(pt, edge_dir)]
    if queued:
        log.info("sweeping %d analyst-queued variant(s): %s",
                 len(queued), ", ".join(pv.name for pv in queued))
    return build_config_grid(base_cfg, proposed=queued), grid_scopes(queued)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep configs over daily history and, if one beats the incumbent "
                    "out-of-sample, edit config.py + emit a PR title/body.")
    parser.add_argument("--tickers", required=True, help="comma-separated")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--config-path", type=Path, default=_CONFIG_PATH)
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    parser.add_argument("--oos-frac", type=float, default=0.3)
    parser.add_argument("--min-oos-trades", type=int, default=MIN_LEADERBOARD_N)
    # None -> the shared env-first resolution (SWING_EDGE_DIR), same as every other CLI.
    parser.add_argument("--edge-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    frames = fetch_daily(tickers, args.cache_dir)
    if not frames:
        # Exit RED, not green: the scheduled weekly run must not read as a successful
        # sweep when a data outage meant nothing was swept at all (2026-07-01 audit).
        log.error("no data fetched for %s; failing the run", tickers)
        raise SystemExit(1)

    grid, scopes = _grid_with_queued(StrategyConfig(), resolve_edge_dir(args.edge_dir))
    result = optimize(frames, timeframe="1d", grid=grid, oos_frac=args.oos_frac,
                      scopes=scopes)
    proposal = propose(result, StrategyConfig(), min_oos_trades=args.min_oos_trades)
    if proposal is None:
        log.info("no config change proposed (no trusted out-of-sample winner over incumbent)")
        return

    args.config_path.write_text(apply_to_config(args.config_path.read_text(encoding="utf-8"),
                                                proposal), encoding="utf-8")
    (args.out_dir / "pr_title.txt").write_text(proposal.title, encoding="utf-8")
    (args.out_dir / "pr_body.md").write_text(proposal.body, encoding="utf-8")
    log.info("proposed: %s", proposal.title)


if __name__ == "__main__":
    main()
