"""The advisory autonomy gate -- the forward-looking "MAY autonomy be considered?" check.

The autonomy gate is the (forward-looking) check that one day says: "conviction is
calibrated AND the edge is proven -> autonomy MAY be considered." It is built NOW but is
**advisory-only** (North Star #1/#3): it emits a reviewable report and **NEVER** mutates
``execution_mode`` or any config -- it is strictly READ-ONLY (it reads the verdicts sidecars
+ the scored ``AnalystCall`` book, and returns a structured verdict). Promotion to ``live``
stays a human act in a later phase.

It mirrors ``pipeline.propose`` (the repo's only other autonomy-shaped, human-gated function):
a statistically HONEST check (North Star #2) -- a real calibration test with TEETH (the
clustered two-sample ``conviction_calibrated``, where a placebo cannot pass), NOT bare means.

``ready`` per play type = (a ``forward_confirmed`` edge exists in its verdicts sidecar) AND
(``conviction_calibrated`` certifies high out-earns low on its scored calls). The OVERALL
``ready`` is True iff AT LEAST ONE play type clears the gate -- one proven, calibrated play
type is enough to start the (still human-gated) autonomy conversation.
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    CalibrationVerdict,
    conviction_calibrated,
)
from swing_screener.db import repo
from swing_screener.db.session import get_engine
from swing_screener.pipeline.proposed import PLAY_TYPES
from swing_screener.pipeline.reflect import Verdict, load_verdicts, verdicts_filename
from swing_screener.settings import load_settings, resolve_edge_dir

log = logging.getLogger(__name__)

# The proven-edge tier: a verdict that cleared the multiple-comparisons-corrected lower
# bound on the LIVE forward shadow book (gold, not a backtest screen).
_CONFIRMED_TIER = "forward_confirmed"
_EDGE_DIR = Path("edge")


@dataclass(frozen=True)
class AutonomyReport:
    """The advisory autonomy verdict (read-only). ``ready`` is True iff at least one play
    type has BOTH a forward-confirmed edge AND calibrated conviction. ``per_play_type`` maps
    each play type to its sub-verdict (``edge_confirmed`` / ``calibrated`` / ``ready`` + the
    ``CalibrationVerdict`` stats + the confirmed-edge conditions). ``blocking_reasons`` lists
    what stands between the book and autonomy, so the report reads as a checklist."""

    ready: bool
    per_play_type: dict[str, dict[str, Any]]
    blocking_reasons: list[str]


def _confirmed_edges(verdicts: list[Verdict]) -> list[Verdict]:
    """The forward-confirmed (proven, live-confirmed) verdicts among ``verdicts``."""
    return [v for v in verdicts if v.tier == _CONFIRMED_TIER]


def _read_verdicts(edge_dir: Path, play_type: str) -> list[Verdict]:
    """Parse ``edge/<pt>.verdicts.json`` (the code-owned sidecar), or [] if it is missing.
    READ-ONLY: a missing or unreadable sidecar yields no proven edge, never an exception
    that would block the advisory run."""
    path = edge_dir / verdicts_filename(play_type)
    if not path.exists():
        return []
    try:
        return load_verdicts(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError):
        log.warning("could not parse verdicts sidecar %s; treating as no proven edge", path)
        return []


def autonomy_gate(session: Session, *, edge_dir: Path = _EDGE_DIR) -> AutonomyReport:
    """Evaluate the advisory autonomy gate over every play type. READ-ONLY.

    For each play type: read ``edge/<pt>.verdicts.json`` -> is there a ``forward_confirmed``
    verdict (a proven edge)? + ``conviction_calibrated`` over its SCORED ``AnalystCall``s
    (does high out-earn low, with the clustered two-sample teeth?). A play type is ``ready``
    iff BOTH hold; the overall report is ``ready`` iff at least one play type is.

    This function performs NO writes: it only reads the verdicts sidecars and the scored-call
    book via ``repo.load_scored_analyst_calls`` (a SELECT). It NEVER mutates ``execution_mode``,
    settings, or any config -- promotion to live stays a human act."""
    per_play_type: dict[str, dict[str, Any]] = {}
    # Per-play-type shortfalls, collected for EVERY play type so a not-ready report reads as
    # a full checklist. They are only surfaced as OVERALL blocking_reasons when no play type
    # clears the gate -- once one does, the overall gate is ready and nothing is blocking.
    shortfalls: list[str] = []

    for pt in PLAY_TYPES:
        confirmed = _confirmed_edges(_read_verdicts(edge_dir, pt))
        edge_confirmed = bool(confirmed)

        calib: CalibrationVerdict = conviction_calibrated(
            repo.load_scored_analyst_calls(session, play_type=pt)
        )
        pt_ready = edge_confirmed and calib.calibrated

        per_play_type[pt] = {
            "ready": pt_ready,
            "edge_confirmed": edge_confirmed,
            "confirmed_conditions": [f"{v.dimension}={v.bucket}" for v in confirmed],
            "calibrated": calib.calibrated,
            "calibration": calib,
        }
        if not edge_confirmed:
            shortfalls.append(f"{pt}: no forward-confirmed edge yet")
        if not calib.calibrated:
            shortfalls.append(f"{pt}: conviction not calibrated ({calib.reason})")

    ready = any(v["ready"] for v in per_play_type.values())
    # Once the overall gate is ready, there is nothing blocking it; otherwise surface every
    # shortfall so the report is an actionable checklist of what stands before autonomy.
    blocking_reasons = [] if ready else shortfalls
    return AutonomyReport(
        ready=ready, per_play_type=per_play_type, blocking_reasons=blocking_reasons
    )


def _countdown_line(
    pt: str, v: dict[str, Any], *, min_per_bucket: int, cluster_floor: int, sep: str = ": "
) -> str:
    """One play type's progress toward the calibration floors, as ``"<pt><sep>..."``.

    A ready play type shows ``"<pt><sep>ready"``; otherwise the three floor fractions from
    its ``CalibrationVerdict``: scored ``high`` / ``low`` calls vs ``min_per_bucket``, and
    the binding distinct-ticker count (``min(n_clusters_high, n_clusters_low)`` -- the lower
    of the two buckets is what actually gates) vs ``cluster_floor``. ``sep`` is the
    play-type/body separator: ``": "`` for the CLI block, ``" "`` for the one-line status.
    PURE."""
    if v["ready"]:
        return f"{pt}{sep}ready"
    calib: CalibrationVerdict = v["calibration"]
    tickers = min(calib.n_clusters_high, calib.n_clusters_low)
    return (
        f"{pt}{sep}{calib.n_high}/{min_per_bucket} high, "
        f"{calib.n_low}/{min_per_bucket} low, "
        f"{tickers}/{cluster_floor} tickers"
    )


def gate_countdown(
    report: AutonomyReport,
    *,
    min_per_bucket: int = MIN_LEADERBOARD_N,
    cluster_floor: int = _CLUSTER_FLOOR,
) -> str:
    """The autonomy-gate COUNTDOWN: per play type, progress toward the calibration floors.

    One line per play type (``"continuation: 7/20 high, 3/20 low, 5/8 tickers"``), or
    ``"<pt>: ready"`` for a play type that clears the gate. The denominators default to the
    live floor constants (``MIN_LEADERBOARD_N`` / ``_CLUSTER_FLOOR``) -- they are NOT
    hardcoded here, so a floor change propagates. PURE (string-building only)."""
    return "\n".join(
        _countdown_line(pt, v, min_per_bucket=min_per_bucket, cluster_floor=cluster_floor)
        for pt, v in report.per_play_type.items()
    )


def gate_status_line(
    report: AutonomyReport,
    *,
    min_per_bucket: int = MIN_LEADERBOARD_N,
    cluster_floor: int = _CLUSTER_FLOOR,
) -> str:
    """The one-liner digest status: the gate verdict + the countdown, on a single line.

    ``"Autonomy gate: READY (advisory)"`` when the gate is ready; otherwise
    ``"Autonomy gate: NOT READY — continuation 7/20 high, ...; reversal 2/20 high, ..."`` --
    the per-play-type progress (same fractions as ``gate_countdown``, joined with ``;`` so it
    stays one line for the digest footer). Reads the live floors. PURE."""
    if report.ready:
        return "Autonomy gate: READY (advisory)"
    progress = "; ".join(
        _countdown_line(
            pt, v, min_per_bucket=min_per_bucket, cluster_floor=cluster_floor, sep=" ")
        for pt, v in report.per_play_type.items()
    )
    return f"Autonomy gate: NOT READY — {progress}"


def render_report(report: AutonomyReport) -> str:
    """Render the advisory autonomy report as human-readable text (for the CLI / a surfaced
    status line). PURE. Always carries the advisory disclaimer -- the gate only advises; it
    never flips ``execution_mode`` or arms anything."""
    headline = "READY (advisory)" if report.ready else "NOT READY"
    lines = [
        "# Autonomy gate (advisory)",
        "",
        f"Status: {headline}",
        "",
        ("This is an ADVISORY report. It NEVER changes execution_mode or any config; "
        "autonomy is switched on last, by a human."),
        "",
    ]
    for pt, v in report.per_play_type.items():
        calib: CalibrationVerdict = v["calibration"]
        edge_mark = "yes" if v["edge_confirmed"] else "no"
        cal_mark = "yes" if v["calibrated"] else "no"
        ready_mark = "READY" if v["ready"] else "blocked"
        lines.append(f"## {pt} -- {ready_mark}")
        conditions = ", ".join(v["confirmed_conditions"]) or "none"
        lines.append(f"- forward-confirmed edge: {edge_mark} ({conditions})")
        lines.append(
            f"- conviction calibrated: {cal_mark} "
            f"(high-low {calib.high_minus_low:+.2f}R, CI low {calib.ci_low:+.2f}, "
            f"n_high={calib.n_high}, n_low={calib.n_low}; {calib.reason})"
        )
        lines.append("")
    # The COUNTDOWN: progress toward the calibration floors per play type, so the report
    # reads as a watchable approach to the gate rather than a bare pass/fail.
    lines.append("## Calibration progress")
    lines.extend(f"- {line}" for line in gate_countdown(report).split("\n"))
    lines.append("")
    if report.blocking_reasons:
        lines.append("## Blocking reasons")
        lines.extend(f"- {r}" for r in report.blocking_reasons)
    else:
        lines.append("No blocking reasons: at least one play type clears the advisory gate.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Advisory autonomy gate: read each play type's forward-confirmed "
                    "verdicts + scored analyst calls and report whether autonomy MAY be "
                    "considered. READ-ONLY -- it never flips execution_mode or any config.")
    # None -> the shared env-first resolution (SWING_EDGE_DIR), so the CLI reads the
    # SAME directory as the digest instead of a second cwd-relative default.
    parser.add_argument("--edge-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = load_settings()
    engine = get_engine(settings.db_url)
    with Session(engine) as session:
        report = autonomy_gate(session, edge_dir=resolve_edge_dir(args.edge_dir))
    print(render_report(report))


if __name__ == "__main__":
    main()
