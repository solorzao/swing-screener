"""The autonomy-gate COUNTDOWN (``pipeline.autonomy.gate_countdown`` / ``gate_status_line``).

The gate can't pass for months (it needs enough scored ``AnalystCall``s). These pure
helpers surface the PROGRESS toward the calibration floors as a watchable countdown --
``"7/20 high, 3/20 low, 5/8 tickers"`` per play type -- so the user can watch the gate
approach instead of guessing. The floors are READ from the real constants
(``MIN_LEADERBOARD_N`` / ``_CLUSTER_FLOOR``), never hardcoded, so a floor change can never
silently drift the countdown out of sync. PURE: these build strings from an
``AutonomyReport`` and touch no I/O.
"""

from typing import Any

from swing_screener.analytics.calibration import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    CalibrationVerdict,
)
from swing_screener.pipeline.autonomy import (
    AutonomyReport,
    gate_countdown,
    gate_status_line,
)


# --- fixtures ----------------------------------------------------------------
def _verdict(
    *, calibrated=False, n_high=7, n_low=3, nc_high=5, nc_low=6
) -> CalibrationVerdict:
    return CalibrationVerdict(
        calibrated=calibrated, high_minus_low=1.1,
        ci_low=0.2 if calibrated else float("-inf"),
        n_high=n_high, n_low=n_low, n_clusters_high=nc_high, n_clusters_low=nc_low,
        reason="ok" if calibrated else "insufficient data: ...",
    )


def _pt_entry(verdict: CalibrationVerdict, *, ready=False) -> dict[str, Any]:
    return {
        "ready": ready, "edge_confirmed": ready, "confirmed_conditions": [],
        "calibrated": verdict.calibrated, "calibration": verdict,
    }


def _report(per_play_type: dict[str, dict[str, Any]], *, ready=False) -> AutonomyReport:
    return AutonomyReport(
        ready=ready, per_play_type=per_play_type,
        blocking_reasons=[] if ready else ["blocked"],
    )


# --- gate_countdown (per play type) ------------------------------------------
def test_countdown_progress_per_play_type() -> None:
    report = _report({
        "continuation": _pt_entry(_verdict(n_high=7, n_low=3, nc_high=5, nc_low=6)),
        "reversal": _pt_entry(_verdict(n_high=2, n_low=1, nc_high=1, nc_low=4)),
    })
    out = gate_countdown(report)
    # min(nc_high, nc_low) is the tickers numerator: continuation -> 5, reversal -> 1.
    assert "continuation: 7/20 high, 3/20 low, 5/8 tickers" in out
    assert "reversal: 2/20 high, 1/20 low, 1/8 tickers" in out
    # One line per play type.
    assert out.count("\n") == 1


def test_countdown_reports_ready_for_a_calibrated_play_type() -> None:
    report = _report(
        {"continuation": _pt_entry(
            _verdict(calibrated=True, n_high=40, n_low=30, nc_high=10, nc_low=9),
            ready=True)},
        ready=True,
    )
    out = gate_countdown(report)
    assert "continuation: ready" in out.lower()


def test_countdown_reads_the_real_floors_not_hardcoded() -> None:
    # The denominators are the live constants; assert against them so a floor change
    # propagates rather than drifting out of sync with a literal 20/8.
    report = _report({"continuation": _pt_entry(_verdict(n_high=7, n_low=3, nc_high=5))})
    out = gate_countdown(report)
    assert f"7/{MIN_LEADERBOARD_N} high" in out
    assert f"3/{MIN_LEADERBOARD_N} low" in out
    assert f"5/{_CLUSTER_FLOOR} tickers" in out


# --- gate_status_line (digest one-liner) -------------------------------------
def test_status_line_not_ready_is_the_progress_one_liner() -> None:
    report = _report({
        "continuation": _pt_entry(_verdict(n_high=7, n_low=3, nc_high=5, nc_low=6)),
        "reversal": _pt_entry(_verdict(n_high=2, n_low=1, nc_high=2, nc_low=4)),
    })
    line = gate_status_line(report)
    assert line.startswith("Autonomy gate: NOT READY")
    assert "continuation 7/20 high, 3/20 low, 5/8 tickers" in line
    assert "reversal 2/20 high, 1/20 low, 2/8 tickers" in line
    assert "\n" not in line  # one line for the digest footer


def test_status_line_ready_is_advisory() -> None:
    report = _report(
        {"continuation": _pt_entry(
            _verdict(calibrated=True, n_high=40, n_low=30, nc_high=10, nc_low=9),
            ready=True)},
        ready=True,
    )
    line = gate_status_line(report)
    assert line == "Autonomy gate: READY (advisory)"
