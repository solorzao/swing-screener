"""Tests for the machine-readable edge-verdicts sidecar (Phase-2 Task 1).

Phase 2's per-pick insight engine derives a deterministic baseline conviction from the
reflection grader's verdicts. It must read a MACHINE-READABLE artifact, never parse the
LLM-authored ``edge/<pt>.md`` prose (which the model rewrites). So the reflection ALSO
emits ``edge/<pt>.verdicts.json`` -- a code-owned, lossless serialization of exactly the
``Verdict`` rows ``grade()`` produced.

Two pieces:
  * ``verdicts_to_json`` / ``load_verdicts`` round-trip EVERY field of EVERY ``Verdict``
    losslessly (``load_verdicts(verdicts_to_json(vs)) == vs``).
  * ``run_reflection`` writes the sidecar next to the markdown for each reflected play
    type, and it deserializes back to exactly what ``grade()`` produced -- deterministic,
    independent of whether the LLM authoring succeeded.

Reuses the in-memory-DB / temp-edge-dir / synthetic-replay / fake-LLM setup pattern from
``test_reflect_run.py`` so the sidecar test exercises the real ``run_reflection`` path.
"""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import Base, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import (
    _REFLECT_TRIGGER_N,
    _REPLAY_HAIRCUT_ATR,
    Verdict,
    grade,
    load_verdicts,
    run_reflection,
    verdicts_to_json,
)
from swing_screener.pipeline.replay import replay_book
from swing_screener.pipeline.variants import DEFAULT_VARIANT


def _haircut_cfg() -> StrategyConfig:
    """The exact replay config run_reflection uses (StrategyConfig with the fixed haircut)."""
    return replace(StrategyConfig(), fill_slippage_atr=_REPLAY_HAIRCUT_ATR)

# --- Fake LLM client (canned markdown body), mirroring test_reflect_run.py. -----------


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _FakeClient:
    """Returns a fixed markdown body (no frontmatter) for every ``messages.create``."""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                return _Resp(outer._text)

        return _M()


# --- DB seeding helpers (mirroring test_reflect_run.py) -------------------------------


def _closed_trade(ticker: str, r: float, play_type: str, *, arm=BASELINE,
                  variant=DEFAULT_VARIANT) -> PaperTrade:
    return PaperTrade(
        ticker=ticker, timeframe="1d", horizon="medium", play_type=play_type,
        signal_score=0.8, rank=1, arm=arm, variant=variant, fill_status="filled",
        stop=95.0, target=110.0, risk=5.0, status="closed", realized_r=r, hold_bars=3,
    )


def _seed(session: Session, trades: list[PaperTrade]) -> None:
    session.add_all(trades)
    session.commit()


def _mem_session() -> Session:
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


# Pristine counter-0 scaffold, NOT copied from the repo's edge/ (living documents whose
# counters advance with every merged reflection PR -- see test_reflect_run._PRISTINE_MD).
from tests.pipeline.test_reflect_run import _PRISTINE_MD  # noqa: E402


def _seed_edge_dir(tmp_path: Path) -> Path:
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    for pt in ("continuation", "reversal"):
        (edge_dir / f"{pt}.md").write_text(_PRISTINE_MD.format(pt=pt), encoding="utf-8")
    return edge_dir


def _synth(n=400) -> pd.DataFrame:
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = 50 + 0.15 * t + 1.2 * np.sin(t / 5.0)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.3
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=idx)


# =====================================================================================
# round-trip -- every field of every Verdict is preserved
# =====================================================================================
def test_verdicts_json_round_trip_preserves_all_fields():
    vs = [
        Verdict(
            play_type="continuation", dimension="market_trend", bucket="bull",
            tier="forward_confirmed", n=37, expectancy_r=0.42, ci_low=0.11,
            n_clusters=6, source="forward",
        ),
        Verdict(
            play_type="reversal", dimension="score", bucket="0.70-0.80",
            tier="hunch", n=0, expectancy_r=0.0, ci_low=float("-inf"),
            n_clusters=0, source="none",
        ),
    ]
    assert load_verdicts(verdicts_to_json(vs)) == vs


def test_verdicts_json_empty_round_trips():
    assert load_verdicts(verdicts_to_json([])) == []


# =====================================================================================
# run_reflection -- writes the sidecar next to the markdown, lossless vs grade()
# =====================================================================================
def test_run_reflection_writes_verdicts_json_sidecar(tmp_path):
    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N + 5  # continuation is due; reversal has nothing
    forward = [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)]
    replay_frames = {"S": _synth()}
    with _mem_session() as session:
        _seed(session, forward)
        client = _FakeClient("## Thesis\n\nAuthored prose.\n")
        reflected = run_reflection(
            session, replay_frames=replay_frames, spy_daily=None,
            edge_dir=edge_dir, client=client, today="2026-06-20",
        )
        # The grader is deterministic, so re-running grade() on the SAME inputs
        # run_reflection used (forward book + the 1d replay slice for this play type)
        # reproduces exactly the verdicts that should have been serialized.
        loaded_forward = [
            t for t in session.query(PaperTrade).all() if t.play_type == "continuation"
        ]
        replay_all = replay_book(
            replay_frames, timeframe="1d", base_cfg=_haircut_cfg(),
            variants={DEFAULT_VARIANT: _haircut_cfg()}, spy_daily=None,
        )
    expected = grade(
        "continuation", loaded_forward,
        [t for t in replay_all if t.play_type == "continuation"],
    )
    assert reflected == ["continuation"]
    # The non-due play type gets no sidecar (it was never reflected).
    assert not (edge_dir / "reversal.verdicts.json").exists()
    # The sidecar is written next to the markdown and deserializes back to EXACTLY the
    # verdicts grade() produced -- lossless, every field, every row.
    sidecar = edge_dir / "continuation.verdicts.json"
    assert sidecar.exists()
    assert load_verdicts(sidecar.read_text(encoding="utf-8")) == expected


def test_run_reflection_writes_sidecar_even_when_llm_fails(tmp_path):
    # The sidecar is deterministic + code-owned: a blank LLM reply (forcing the markdown
    # fallback) must NOT prevent the json sidecar from being written.
    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("  "), today=None,
        )
    sidecar = edge_dir / "continuation.verdicts.json"
    assert sidecar.exists()
    assert load_verdicts(sidecar.read_text(encoding="utf-8"))  # non-empty verdict list
