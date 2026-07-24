"""Tests for the machine-readable edge-verdicts sidecar (Phase-2 Task 1).

Phase 2's per-pick insight engine derives a deterministic baseline conviction from the
reflection grader's verdicts. It must read a MACHINE-READABLE artifact, never parse the
LLM-authored ``edge/<pt>.md`` prose (which the model rewrites). So the reflection ALSO
emits ``edge/<pt>.verdicts.json`` -- a code-owned, lossless serialization of exactly the
``Verdict`` rows ``grade()`` produced.

Three pieces:
  * ``verdicts_to_json`` / ``load_verdicts`` round-trip EVERY field of EVERY ``Verdict``
    losslessly (``load_verdicts(verdicts_to_json(vs)) == vs``), and a PRE-provenance
    sidecar (no ``cost_level``/``corpus_id`` keys) still parses with those fields None.
  * ``_stamp_provenance`` (Phase 3) stamps each verdict's cost level + replay corpus id
    post-``grade`` by source: replay rows carry the fixed haircut + the corpus id, forward
    rows carry what the gold cohort provably realized (``cost_level_for``) and NO corpus,
    and ``source='none'`` rows carry neither (an empty cell measured nothing).
  * ``run_reflection`` writes the sidecar next to the markdown for each reflected play
    type, and it deserializes back to exactly the STAMPED ``grade()`` output --
    deterministic, independent of whether the LLM authoring succeeded.

Reuses the in-memory-DB / temp-edge-dir / synthetic-replay / fake-LLM setup pattern from
``test_reflect_run.py`` so the sidecar test exercises the real ``run_reflection`` path.
"""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import COST_STAMPED_FROM
from swing_screener.config import StrategyConfig
from swing_screener.db.models import Base, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import (
    _REFLECT_TRIGGER_N,
    _REPLAY_HAIRCUT_ATR,
    Verdict,
    _stamp_provenance,
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
from tests.pipeline.test_reflect_run import _PRISTINE_MD


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
            n_clusters=6, source="forward", cost_level="0.05", corpus_id=None,
        ),
        Verdict(
            play_type="continuation", dimension="volatility_tier", bucket="high",
            tier="replay_screened", n=120, expectancy_r=0.10, ci_low=0.02,
            n_clusters=11, source="replay", cost_level="0.05",
            corpus_id="corpus: 12/12 tickers · vintage(s) 20260703:12",
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


def test_load_verdicts_tolerates_pre_provenance_sidecar() -> None:
    """A committed sidecar written BEFORE the Phase-3 provenance stamps has no
    ``cost_level``/``corpus_id`` keys. ``load_verdicts`` is a strict ``Verdict(**d)``,
    so the new fields MUST default -- an old sidecar parses with them None."""
    old = json.dumps([{
        "play_type": "continuation", "dimension": "market_trend", "bucket": "bull",
        "tier": "hunch", "n": 2442, "expectancy_r": -0.13, "ci_low": -0.19,
        "n_clusters": 100, "source": "none",
    }])
    (v,) = load_verdicts(old)
    assert v.cost_level is None
    assert v.corpus_id is None
    assert v.bucket == "bull"  # the pre-existing fields still parse


# =====================================================================================
# _stamp_provenance -- per-source stamping rules (post-grade; grade() stays pure)
# =====================================================================================
def _verdict(source: str, tier: str) -> Verdict:
    return Verdict(
        play_type="continuation", dimension="market_trend", bucket="bull",
        tier=tier, n=25, expectancy_r=0.3, ci_low=0.1, n_clusters=25, source=source,
    )


def _cost_proven_trade(ticker: str) -> PaperTrade:
    """A closed-filled forward trade that PROVES the 0.05 cost epoch (exited on/after
    ``COST_STAMPED_FROM``, never partialed)."""
    t = _closed_trade(ticker, 1.0, "continuation")
    t.opened_date = COST_STAMPED_FROM
    t.exit_date = COST_STAMPED_FROM
    t.partial_done = False
    return t


def test_stamp_provenance_per_source_rules() -> None:
    corpus = "corpus: 12/12 tickers · vintage(s) 20260703:12 · pinned as-of 20260703"
    vs = [
        _verdict("forward", "forward_confirmed"),
        _verdict("replay", "replay_screened"),
        _verdict("none", "hunch"),
    ]
    fwd_book = [_cost_proven_trade("A"), _cost_proven_trade("B")]
    forward, rpl, none = _stamp_provenance(vs, fwd_book, corpus_id=corpus)

    # replay rows: the fixed a-priori haircut (frontend-glyph format) + the corpus stamp.
    assert rpl.cost_level == str(_REPLAY_HAIRCUT_ATR) == "0.05"
    assert rpl.corpus_id == corpus
    # forward rows: what the gold cohort provably realized; a forward corpus is a
    # category error, so corpus_id stays None even when a replay corpus id is known.
    assert forward.cost_level == "0.05"
    assert forward.corpus_id is None
    # source='none': an empty cell measured nothing -- no provenance claim at all.
    assert none.cost_level is None
    assert none.corpus_id is None
    # everything else is untouched (dataclasses.replace, not a rebuild).
    assert rpl.tier == "replay_screened" and rpl.n == 25 and rpl.expectancy_r == 0.3


def test_stamp_provenance_forward_follows_cost_level_for_not_the_constant() -> None:
    """A forward cohort with a pre-cutoff (unprovable) exit is a mixed gross/net book:
    ``cost_level_for`` says None, and the forward stamp must follow it -- proving the
    forward rule reads the BOOK, not the replay haircut constant."""
    unproven = _closed_trade("A", 1.0, "continuation")
    unproven.exit_date = None  # cannot prove its cost epoch
    (forward,) = _stamp_provenance(
        [_verdict("forward", "forward_confirmed")], [unproven], corpus_id="corpus: x",
    )
    assert forward.cost_level is None
    assert forward.corpus_id is None


def test_stamp_provenance_replay_corpus_id_none_when_unknown() -> None:
    (rpl,) = _stamp_provenance(
        [_verdict("replay", "replay_screened")], [], corpus_id=None,
    )
    assert rpl.cost_level == "0.05"
    assert rpl.corpus_id is None


def test_stamp_provenance_none_rows_unstamped_even_with_displayed_numbers() -> None:
    """A hunch cell DISPLAYS whichever book had data (n>0) but confirmed nothing --
    stamping it would claim provenance for a non-measurement, so both stay None even
    when a corpus id and a cost-proven forward book are available."""
    hunch = _verdict("none", "hunch")  # carries n=25 display numbers
    (out,) = _stamp_provenance([hunch], [_cost_proven_trade("A")], corpus_id="corpus: x")
    assert out.cost_level is None
    assert out.corpus_id is None
    assert out.n == 25  # the displayed numbers survive untouched


def test_grade_emits_unstamped_verdicts() -> None:
    """``grade`` stays PURE: every verdict it builds carries the default (None) stamps;
    provenance is stamped post-grade by ``_stamp_provenance`` only."""
    fwd = [_cost_proven_trade(f"T{i}") for i in range(25)]
    for t in fwd:
        t.market_trend = "bull"
    verdicts = grade("continuation", fwd, [])
    assert verdicts  # the family is non-empty
    assert all(v.cost_level is None and v.corpus_id is None for v in verdicts)


# =====================================================================================
# run_reflection -- writes the sidecar next to the markdown, lossless vs grade()
# =====================================================================================
def test_run_reflection_writes_verdicts_json_sidecar(tmp_path):
    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N + 5  # continuation is due; reversal has nothing
    forward = [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)]
    for t in forward:
        t.would_surface = True  # the gold facet: reflection grades only surfaced rows
    corpus = "corpus: 1/1 tickers · vintage(s) 20260703:1"
    replay_frames = {"S": _synth()}
    with _mem_session() as session:
        _seed(session, forward)
        client = _FakeClient("## Thesis\n\nAuthored prose.\n")
        reflected = run_reflection(
            session, replay_frames=replay_frames, spy_daily=None,
            edge_dir=edge_dir, client=client, today="2026-06-20", corpus_id=corpus,
        )
        # The grader is deterministic, so re-running grade() on the SAME inputs
        # run_reflection used (forward book + the 1d replay slice for this play type)
        # -- with the same post-grade provenance stamping -- reproduces exactly the
        # verdicts that should have been serialized.
        loaded_forward = [
            t for t in session.query(PaperTrade).all()
            if t.play_type == "continuation" and t.would_surface  # the graded gold facet
        ]
        replay_all = replay_book(
            replay_frames, timeframe="1d", base_cfg=_haircut_cfg(),
            variants={DEFAULT_VARIANT: _haircut_cfg()}, spy_daily=None,
        )
        expected = _stamp_provenance(
            grade(
                "continuation", loaded_forward,
                [t for t in replay_all if t.play_type == "continuation"],
            ),
            loaded_forward, corpus_id=corpus,
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
