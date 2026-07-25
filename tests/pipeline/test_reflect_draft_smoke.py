"""Tests for the proposal path-reachability SMOKE GUARD + the play-type-conditional
drafter prompt (Q10, docs/plans/2026-07-25-strategy-review-experiment-queue.md).

4 of the 5 reversal proposals the reflection drafter ever minted were path-UNREACHABLE:
their deltas touched knobs only the CONTINUATION path reads (``max_extension_atr`` --
pipeline/analyze.py analyze_frames; ``min_pullback_bars`` -- signals/detect.py) or a
tag-only field (``oversold_rsi_max``), so sweeping them replays the byte-identical
reversal book and mints a FALSE NULL on the hunch. ``to_config`` cannot catch this: the
fields exist and are not frozen indicators -- the defect is path reachability, which only
an actual replay can certify. Two fixes under test:

  * PROMPT (Q10 B): ``_draft_tool`` / ``_draft_system`` are play-type-conditional, with
    code-owned example knob lists per play type and an explicit "a knob the other play
    type's path reads will be rejected" warning -- the old prompt listed continuation
    knobs as examples for BOTH play types, which is what primed the no-op drafts.
  * SMOKE GUARD (Q10 C): ``draft_variants(smoke_frames=...)`` replays each surviving
    candidate against the incumbent config on a small corpus and DROPS a candidate whose
    play-type-SCOPED book (canonical per-trade economics tuples) is identical to the
    default's -- when the default book is deep enough to trust the null (>= 30 scoped
    trades). Identical-but-thin queues with a "smoke-inconclusive" warning; a replay
    failure fail-safes to queueing nothing (store untouched).

The acceptance corpus is REAL replay over synthetic frames (no seams): a two-frequency
sinusoid whose slow wave makes multi-bar pullbacks/declines (both play types book
trades) and whose fast wiggle leaves some pullbacks 1-2 HA bars deep, so
``min_pullback_bars``/``max_extension_atr`` demonstrably move the CONTINUATION book on
the very corpus where the reversal-scoped book stays identical -- the pooled-book
control that kills an unscoped-diff implementation. Economics stay in the canonical
tuple so an economics-only delta (``reversal_retrace_frac`` -- same fills, different
target) is NOT flagged unreachable -- the fill-ID-only control.
"""

import logging

import numpy as np
import pandas as pd
import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline import reflect
from swing_screener.pipeline.proposed import QUEUED, load_proposed_for, proposed_to_json
from swing_screener.pipeline.reflect import (
    Verdict,
    _draft_system,
    _draft_tool,
    _opus_drafter,
    draft_variants,
)


def _hunch(*, play_type: str, dimension: str = "volatility_tier", bucket: str = "med") -> Verdict:
    return Verdict(
        play_type=play_type, dimension=dimension, bucket=bucket, tier="hunch",
        n=12, expectancy_r=0.1, ci_low=-0.2, n_clusters=4, source="none",
    )


def _fake_drafter(drafts):
    def _fn(play_type, hunches, base):
        return list(drafts)
    return _fn


def _synth(n: int = 350, *, phase: float = 0.0, slope: float = 0.05,
           wiggle: float = 0.5) -> pd.DataFrame:
    """Two-frequency uptrend: slow wave -> multi-bar pullbacks/declines (books BOTH
    play types); fast wiggle -> some 1-2 bar pullbacks (min_pullback_bars fodder);
    volume wave -> flip-bar volume_ratio spread (reversal_min_flip_rvol fodder)."""
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = (50 + slope * t + 1.2 * np.sin(t / 4.0 + phase)
             + wiggle * np.sin(t / 1.3 + 2 * phase))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.3
    vol = 1e6 * (1.0 + 0.8 * np.sin(t / 3.0 + phase))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.clip(vol, 1e5, None)}, index=idx)


def _acceptance_corpus() -> dict[str, pd.DataFrame]:
    """5 tickers x 350 bars: books >= 30 default trades for EACH play type (deep enough
    for the guard to trust an identical-book null), with the knob-response properties
    the module docstring describes."""
    return {f"T{i}": _synth(phase=i * 0.9, slope=0.03 + 0.025 * i,
                            wiggle=0.3 + 0.1 * i)
            for i in range(5)}


# The four historical no-op deltas, verbatim from edge/reversal.proposed.json's
# withdrawal records (2026-07-03 x3 + the 2026-07-16 re-mint withdrawn 2026-07-25).
_HISTORICAL_NOOPS = [
    {"delta": {"max_extension_atr": 3.0}, "rationale": "tighten ext gate",
     "hunch_ref": "volatility_tier=high"},
    {"delta": {"min_pullback_bars": 3}, "rationale": "deeper pullback",
     "hunch_ref": "market_trend=bull"},
    {"delta": {"oversold_rsi_max": 25}, "rationale": "deeper oversold",
     "hunch_ref": "score=0.00-0.50"},
    {"delta": {"max_extension_atr": 2.5}, "rationale": "cap extension",
     "hunch_ref": "volatility_tier=med"},
]


# =====================================================================================
# Acceptance (Q10 D): the four historical no-ops REJECTED, reachable deltas ACCEPTED
# =====================================================================================
def test_all_four_historical_noop_reversal_deltas_are_dropped_unreachable(
    tmp_path, caplog,
):
    # The two REACHABLE reversal deltas ride along in the same draft: an economics-only
    # one (reversal_retrace_frac moves the target, not the fills -- kills a fill-ID-only
    # canonicalization) and a detection gate (reversal_min_flip_rvol changes membership).
    base = StrategyConfig()
    reachable = [
        {"delta": {"reversal_retrace_frac": 0.618}, "rationale": "golden-ratio target",
         "hunch_ref": "strength=confirmed"},
        {"delta": {"reversal_min_flip_rvol": 1.3}, "rationale": "high-vol flips only",
         "hunch_ref": "volatility_tier=high"},
    ]
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "reversal", [_hunch(play_type="reversal")], base, edge_dir=tmp_path,
            today="2026-07-26", drafter=_fake_drafter(_HISTORICAL_NOOPS + reachable),
            smoke_frames=_acceptance_corpus(),
        )
    assert [pv.delta for pv in written] == [
        {"reversal_retrace_frac": 0.618}, {"reversal_min_flip_rvol": 1.3},
    ]
    assert load_proposed_for("reversal", tmp_path) == written
    assert all(pv.status == QUEUED for pv in written)
    # Every dropped no-op is warned as unreachable, naming its knob.
    unreachable_msgs = [r.message for r in caplog.records
                       if "unreachable" in r.message.lower()]
    assert len(unreachable_msgs) == 4
    for knob in ("max_extension_atr", "min_pullback_bars", "oversold_rsi_max"):
        assert any(knob in m for m in unreachable_msgs)


def test_same_knob_is_reachable_for_continuation_on_the_same_corpus(tmp_path):
    # The pooled-book control's complement: on the SAME corpus, max_extension_atr and
    # min_pullback_bars DO move the continuation book -- so their rejection above can
    # only come from play-type SCOPING, not from a dead corpus. An unscoped diff would
    # have (wrongly) accepted them for reversal; an over-scoped guard would (wrongly)
    # reject them here.
    base = StrategyConfig()
    written = draft_variants(
        "continuation", [_hunch(play_type="continuation")], base, edge_dir=tmp_path,
        today="2026-07-26",
        drafter=_fake_drafter([
            {"delta": {"min_pullback_bars": 3}, "rationale": "deeper pullback",
             "hunch_ref": "market_trend=bull"},
            {"delta": {"max_extension_atr": 2.5}, "rationale": "looser ext gate",
             "hunch_ref": "volatility_tier=med"},
        ]),
        smoke_frames=_acceptance_corpus(),
    )
    assert [pv.delta for pv in written] == [
        {"min_pullback_bars": 3}, {"max_extension_atr": 2.5},
    ]
    assert load_proposed_for("continuation", tmp_path) == written


# =====================================================================================
# The thin-corpus, fail-safe, and opt-in seams
# =====================================================================================
def test_thin_smoke_corpus_queues_with_smoke_inconclusive_warning(tmp_path, caplog):
    # One ticker books far fewer than 30 default reversal trades: an identical book on
    # a corpus this thin proves nothing, so the candidate queues -- flagged, not dropped.
    base = StrategyConfig()
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "reversal", [_hunch(play_type="reversal")], base, edge_dir=tmp_path,
            today="2026-07-26",
            drafter=_fake_drafter([
                {"delta": {"max_extension_atr": 2.5}, "rationale": "cap extension",
                 "hunch_ref": "volatility_tier=med"},
            ]),
            smoke_frames={"T0": _synth(300)},
        )
    assert len(written) == 1
    assert load_proposed_for("reversal", tmp_path) == written
    assert any("smoke-inconclusive" in r.message for r in caplog.records)


def test_smoke_replay_failure_queues_nothing_and_leaves_store_untouched(
    tmp_path, caplog, monkeypatch,
):
    # Fail-safe mirrors every other drafting failure: a raising smoke replay queues
    # NOTHING and leaves the prior store bytes exactly as found.
    base = StrategyConfig()
    prior = proposed_to_json([])
    (tmp_path / "reversal.proposed.json").write_text(prior, encoding="utf-8")

    def _boom(*a, **kw):
        raise RuntimeError("replay exploded")

    monkeypatch.setattr(reflect, "replay_book", _boom)
    with caplog.at_level(logging.WARNING):
        written = draft_variants(
            "reversal", [_hunch(play_type="reversal")], base, edge_dir=tmp_path,
            today="2026-07-26",
            drafter=_fake_drafter([
                {"delta": {"reversal_min_flip_rvol": 1.3}, "rationale": "r",
                 "hunch_ref": "h"},
            ]),
            smoke_frames={"T0": _synth(260)},
        )
    assert written == []
    assert (tmp_path / "reversal.proposed.json").read_text(encoding="utf-8") == prior
    assert any("smoke" in r.message.lower() and "queueing nothing" in r.message
               for r in caplog.records)


def test_no_smoke_frames_skips_the_guard(tmp_path):
    # The guard is an opt-in seam (run_reflection wires it): without smoke_frames the
    # historical behavior is byte-identical -- even an unreachable delta queues.
    base = StrategyConfig()
    written = draft_variants(
        "reversal", [_hunch(play_type="reversal")], base, edge_dir=tmp_path,
        today="2026-07-26",
        drafter=_fake_drafter([
            {"delta": {"max_extension_atr": 2.5}, "rationale": "cap extension",
             "hunch_ref": "volatility_tier=med"},
        ]),
    )
    assert len(written) == 1


# =====================================================================================
# PROMPT (Q10 B): play-type-conditional tool/system with code-owned knob examples
# =====================================================================================
def test_draft_tool_and_system_are_play_type_conditional():
    rev_tool, cont_tool = _draft_tool("reversal"), _draft_tool("continuation")
    rev_sys, cont_sys = _draft_system("reversal"), _draft_system("continuation")
    # Reversal prompts name reversal-path knobs and never the continuation examples
    # that primed the historical no-ops; continuation prompts are the mirror image.
    for text in (rev_tool["description"], rev_sys):
        assert "reversal_min_flip_rvol" in text
        assert "max_extension_atr" not in text
        assert "min_pullback_bars" not in text
    for text in (cont_tool["description"], cont_sys):
        assert "max_extension_atr" in text
        assert "reversal_min_flip_rvol" not in text
    # Both carry the explicit wrong-play-type rejection warning.
    for text in (rev_sys, cont_sys):
        assert "rejected" in text
    # The tool NAME stays stable (the extractor keys on it).
    assert rev_tool["name"] == cont_tool["name"] == "propose_screen_variants"


def test_opus_drafter_sends_the_play_type_conditional_prompt():
    captured = {}

    class _Msgs:
        def create(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop after capture")

    class _Client:
        messages = _Msgs()

    fn = _opus_drafter(client=_Client())
    with pytest.raises(RuntimeError, match="stop after capture"):
        fn("reversal", [_hunch(play_type="reversal")], StrategyConfig())
    assert captured["system"] == _draft_system("reversal")
    assert captured["tools"] == [_draft_tool("reversal")]


# =====================================================================================
# run_reflection wires the guard: a capped, deterministic subsample of replay_frames
# =====================================================================================
def test_run_reflection_passes_a_capped_smoke_subsample(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session

    from swing_screener.db.models import Base
    from swing_screener.db.session import get_engine
    from tests.pipeline.test_reflect_run import _PRISTINE_MD
    from tests.pipeline.test_reflect_verdicts_json import _FakeClient

    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    for pt in ("continuation", "reversal"):
        (edge_dir / f"{pt}.md").write_text(_PRISTINE_MD.format(pt=pt), encoding="utf-8")

    calls = []

    def _recorder(play_type, hunches, base, *, edge_dir, today, drafter,
                  smoke_frames=None):
        calls.append((play_type, smoke_frames))
        return []

    monkeypatch.setattr(reflect, "draft_variants", _recorder)
    monkeypatch.setattr(reflect, "_SMOKE_TICKERS", 2)
    frames = {"C": _synth(260), "A": _synth(260, phase=1.0), "B": _synth(260, phase=2.0)}

    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        reflect.run_reflection(
            session, replay_frames=frames, spy_daily=None, edge_dir=edge_dir,
            client=_FakeClient("## Thesis\n\nAuthored prose.\n"), today="2026-07-26",
            drafter=_fake_drafter([]), force=True,
        )
    assert [pt for pt, _ in calls] == ["continuation", "reversal"]
    # The subsample is the first _SMOKE_TICKERS tickers in sorted order -- capped and
    # deterministic, so the weekly smoke certifies against a stable corpus.
    for _, smoke in calls:
        assert list(smoke) == ["A", "B"]
