"""The advisory autonomy gate (``pipeline.autonomy``).

The gate is the forward-looking check that one day says "conviction is calibrated AND the
edge is proven -> autonomy MAY be considered." It is built NOW but is ADVISORY-ONLY: it
emits a reviewable report and NEVER mutates ``execution_mode`` or any config (North Star
#1). The tests pin both the ``ready`` logic (a forward_confirmed edge AND calibrated
conviction, per play type) AND the read-only property (it writes nothing to the DB / no
config / no settings).
"""

from datetime import date

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, ExecutionLog
from swing_screener.db.session import get_engine
from swing_screener.pipeline.autonomy import (
    AutonomyReport,
    autonomy_gate,
    render_report,
)
from swing_screener.pipeline.reflect import Verdict, verdicts_to_json


# --- fixtures ----------------------------------------------------------------
def _session() -> Session:
    return Session(get_engine("sqlite:///:memory:"))


def _verdict(tier: str, *, dimension="market_trend", bucket="bull") -> Verdict:
    return Verdict(
        play_type="continuation", dimension=dimension, bucket=bucket, tier=tier,
        n=30, expectancy_r=0.6, ci_low=0.2, n_clusters=10, source="forward",
    )


def _write_verdicts(edge_dir, play_type: str, verdicts: list[Verdict]) -> None:
    (edge_dir / f"{play_type}.verdicts.json").write_text(
        verdicts_to_json(verdicts), encoding="utf-8"
    )


def _call(ticker: str, conviction: str, r: float, *, play_type="continuation") -> AnalystCall:
    return AnalystCall(
        created_date=date(2026, 6, 1), ticker=ticker, timeframe="1d",
        play_type=play_type, run_date=date(2026, 6, 1),
        baseline_conviction="medium", final_conviction=conviction,
        nudge_reason="x", model="claude-opus-4-8",
        realized_r=r, scored_at=date(2026, 6, 5),
    )


def _calibrated_calls(play_type="continuation") -> list[AnalystCall]:
    """A clean calibrated book for one play type: high clearly out-earns low, deep, across
    >= 8 distinct tickers (so ``conviction_calibrated`` certifies it)."""
    calls: list[AnalystCall] = []
    for i in range(8):
        for r in (1.2, 1.4, 1.3, 1.2, 1.4):
            calls.append(_call(f"H{play_type}{i}", "high", r, play_type=play_type))
        for r in (-0.1, 0.0, -0.2, 0.1, -0.1):
            calls.append(_call(f"L{play_type}{i}", "low", r, play_type=play_type))
    return calls


# --- ready logic -------------------------------------------------------------
def test_ready_when_forward_confirmed_edge_and_conviction_calibrated(tmp_path) -> None:
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = autonomy_gate(s, edge_dir=edge_dir)
    assert isinstance(report, AutonomyReport)
    assert report.ready is True
    assert report.blocking_reasons == []
    pt = report.per_play_type["continuation"]
    assert pt["edge_confirmed"] is True
    assert pt["calibrated"] is True


def test_not_ready_when_edge_unconfirmed(tmp_path) -> None:
    # Calibrated conviction, but only a HUNCH verdict -> no proven edge -> not ready.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("hunch")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = autonomy_gate(s, edge_dir=edge_dir)
    assert report.ready is False
    assert report.per_play_type["continuation"]["edge_confirmed"] is False
    assert any("edge" in r.lower() for r in report.blocking_reasons)


def test_not_ready_when_conviction_not_calibrated(tmp_path) -> None:
    # Proven edge, but NO scored analyst calls -> conviction not calibrated -> not ready.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    with _session() as s:
        report = autonomy_gate(s, edge_dir=edge_dir)
    assert report.ready is False
    assert report.per_play_type["continuation"]["calibrated"] is False
    assert any("calibrat" in r.lower() or "data" in r.lower()
               for r in report.blocking_reasons)


def test_not_ready_when_no_verdicts_files(tmp_path) -> None:
    # No edge files at all (fresh repo) -> nothing proven -> not ready, no crash.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    with _session() as s:
        report = autonomy_gate(s, edge_dir=edge_dir)
    assert report.ready is False
    assert report.blocking_reasons  # at least one reason recorded


def test_ready_for_one_play_type_is_enough(tmp_path) -> None:
    # continuation is fully gated (confirmed + calibrated); reversal is not. Overall
    # ``ready`` is True because AT LEAST ONE play type clears the gate.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    _write_verdicts(edge_dir, "reversal", [_verdict("hunch")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = autonomy_gate(s, edge_dir=edge_dir)
    assert report.ready is True
    assert report.per_play_type["continuation"]["ready"] is True
    assert report.per_play_type["reversal"]["ready"] is False


# --- read-only / never-arms property (load-bearing, North Star #1) -----------
def test_gate_writes_nothing_to_the_db(tmp_path) -> None:
    """The gate must be READ-ONLY: running it adds NO rows (no ExecutionLog, no settings
    mutation) and leaves the AnalystCall rows untouched."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        before_calls = s.query(AnalystCall).count()
        before_logs = s.query(ExecutionLog).count()

        autonomy_gate(s, edge_dir=edge_dir)

        # No new rows of ANY execution-shaped kind; the call book is unchanged; and the
        # session has no pending writes queued (nothing dirty/new/deleted).
        assert s.query(AnalystCall).count() == before_calls
        assert s.query(ExecutionLog).count() == before_logs
        assert before_logs == 0
        assert not s.new and not s.dirty and not s.deleted


def test_gate_does_not_touch_execution_mode_setting(tmp_path, monkeypatch) -> None:
    """The gate NEVER flips ``execution_mode``: the env var it would read stays whatever it
    was, and the gate exposes no setter. (It takes only a session + an edge dir.)"""
    monkeypatch.setenv("SWING_EXECUTION_MODE", "off")
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        autonomy_gate(s, edge_dir=edge_dir)
    import os
    assert os.environ["SWING_EXECUTION_MODE"] == "off"   # untouched by the gate


def test_gate_does_not_create_or_modify_edge_files(tmp_path) -> None:
    # The gate READS the verdicts sidecars; it must not write/rewrite any edge file.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    before = {p.name: p.read_bytes() for p in edge_dir.iterdir()}
    with _session() as s:
        autonomy_gate(s, edge_dir=edge_dir)
    after = {p.name: p.read_bytes() for p in edge_dir.iterdir()}
    assert before == after   # no file created, deleted, or modified


# --- render ------------------------------------------------------------------
def test_render_report_is_human_readable(tmp_path) -> None:
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    _write_verdicts(edge_dir, "continuation", [_verdict("forward_confirmed")])
    with _session() as s:
        s.add_all(_calibrated_calls("continuation"))
        s.commit()
        report = autonomy_gate(s, edge_dir=edge_dir)
    text = render_report(report)
    assert "continuation" in text
    # The headline verdict + the advisory disclaimer must both be visible.
    assert "READY" in text.upper()
    assert "advisory" in text.lower() or "never" in text.lower()


def test_no_db_schema_surprise_columns_for_execution_mode() -> None:
    # Defensive: there is no ``settings``/``execution_mode`` DB table the gate could write
    # (the master switch is env-driven). Asserting it absent documents the read-only design.
    engine = get_engine("sqlite:///:memory:")
    tables = sa_inspect(engine).get_table_names()
    assert "settings" not in tables
