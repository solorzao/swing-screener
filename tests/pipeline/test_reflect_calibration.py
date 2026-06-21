"""Analyst calibration in the reflection (Task 6, Part D).

Closes the learning loop: the reflection summarizes a play type's SCORED
``AnalystCall`` rows -- does its ``high`` conviction out-earn its ``low``? do its
nudges (final != baseline) add R vs. the baseline? -- and surfaces it as a
deterministic, code-owned "Analyst calibration" note in the edge file.

The summary helper is PURE (no DB, no LLM); the note rendering is deterministic;
``run_reflection`` writes the note into the rewritten ``edge/<pt>.md``.
"""

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.db.models import AnalystCall, Base, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import (
    _CALIBRATION_HEADER,
    _REFLECT_TRIGGER_N,
    analyst_calibration,
    render_calibration_note,
    run_reflection,
)
from swing_screener.pipeline.variants import DEFAULT_VARIANT


def _call(grade_final: str, baseline: str, r: float | None, *, play_type="continuation",
          scored=True) -> AnalystCall:
    return AnalystCall(
        created_date=date(2026, 6, 1), ticker="AMD", timeframe="1d", play_type=play_type,
        run_date=date(2026, 6, 1), baseline_conviction=baseline, final_conviction=grade_final,
        nudge_reason="x", model="claude-opus-4-8", realized_r=r,
        scored_at=date(2026, 6, 5) if scored else None,
    )


# --- analyst_calibration (pure) ----------------------------------------------
def test_calibration_buckets_by_final_conviction_with_mean_r() -> None:
    calls = [
        _call("high", "high", 2.0), _call("high", "high", 1.0),   # high: n=2 mean 1.5
        _call("low", "low", -1.0),                                 # low:  n=1 mean -1.0
    ]
    calib = analyst_calibration(calls)
    assert calib["by_conviction"]["high"] == (2, 1.5)
    assert calib["by_conviction"]["low"] == (1, -1.0)
    assert "medium" not in calib["by_conviction"]  # no scored medium calls


def test_calibration_nudge_vs_baseline_r() -> None:
    # Two NUDGED calls (final != baseline) -- their mean R is the nudge signal. A call
    # that agreed with the baseline (final == baseline) is excluded from the nudge stat.
    calls = [
        _call("high", "medium", 3.0),   # nudged up
        _call("low", "medium", -1.0),   # nudged down
        _call("medium", "medium", 5.0),  # AGREED -> excluded from nudge stat
    ]
    calib = analyst_calibration(calls)
    assert calib["nudge_vs_baseline_r"] == (2, 1.0)   # (n_nudges, mean R) = (3-1)/2


def test_calibration_ignores_unscored_calls() -> None:
    calls = [_call("high", "high", None, scored=False), _call("high", "high", 2.0)]
    calib = analyst_calibration(calls)
    assert calib["by_conviction"]["high"] == (1, 2.0)   # only the scored one counts


def test_calibration_empty_when_no_scored_calls() -> None:
    calib = analyst_calibration([_call("high", "high", None, scored=False)])
    assert calib["by_conviction"] == {}
    assert calib["nudge_vs_baseline_r"] is None


# --- render_calibration_note (deterministic) ---------------------------------
def test_render_note_lists_each_conviction_and_the_nudge_line() -> None:
    calib = analyst_calibration([
        _call("high", "medium", 2.0), _call("low", "low", -0.5),
    ])
    note = render_calibration_note(calib)
    assert "high" in note and "low" in note
    assert "+2.00R" in note or "2.00R" in note
    assert "nudge" in note.lower()


def test_render_note_no_scored_calls_is_placeholder() -> None:
    note = render_calibration_note(analyst_calibration([]))
    assert note.strip()  # not empty -- a clear "no scored calls yet" placeholder


# --- run_reflection: the note appears in the rewritten edge file -------------
class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    def __init__(self, text):
        self._text = text

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                return _Resp(outer._text)

        return _M()


def _closed_trade(ticker, r, play_type="continuation") -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", play_type=play_type,
        signal_score=0.8, rank=1, arm=BASELINE, variant=DEFAULT_VARIANT,
        fill_status="filled", stop=95.0, target=110.0, risk=5.0, status="closed",
        realized_r=r, hold_bars=3,
    )


def _synth(n=400) -> pd.DataFrame:
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = 50 + 0.15 * t + 1.2 * np.sin(t / 5.0)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.3
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=idx)


def _mem_session() -> Session:
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_run_reflection_surfaces_calibration_note(tmp_path):
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    n = _REFLECT_TRIGGER_N
    with _mem_session() as session:
        session.add_all([_closed_trade(f"T{i}", 1.0) for i in range(n)])
        # A scored analyst call for continuation -> its calibration must surface.
        session.add(_call("high", "medium", 2.5))
        session.commit()
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient(""),  # blank -> deterministic render
            today="2026-06-20",
        )
    out = (edge_dir / "continuation.md").read_text(encoding="utf-8")
    assert _CALIBRATION_HEADER in out
    assert "high" in out
