"""Tests for the event trigger + the run orchestration of ``pipeline.reflect`` (Task 6).

Two load-bearing pieces, both exercised WITHOUT network or a real LLM:

  * ``due_play_types`` is the EVENT TRIGGER -- a play type is due iff its FORWARD
    closed-book count has advanced by ``>= _REFLECT_TRIGGER_N`` since the counter the
    edge file's frontmatter records (0 when the file is missing). It reads the live
    forward book at (arm=BASELINE, variant=DEFAULT_VARIANT) -- the same facet the grader
    grades -- so the trigger and the grade never disagree about "what's new".
  * ``run_reflection`` rewrites only the DUE play types' ``edge/<pt>.md`` (no DB writes,
    no level changes): it grades forward-vs-replay, authors the markdown via the injectable
    LLM seam, and stamps the FORWARD count into the frontmatter so the NEXT trigger measures
    from here. A non-due play type's file is left byte-for-byte untouched.

``main`` is thin glue (settings -> engine -> Session -> fetch seams -> run_reflection); a
single smoke test monkeypatches the fetch seams + DB so no network/real-LLM is hit.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from swing_screener.db.models import Base, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.reflect import (
    _DEFAULT_THESIS,
    _REFLECT_TRIGGER_N,
    due_play_types,
    parse_state,
    run_reflection,
)
from swing_screener.pipeline.variants import DEFAULT_VARIANT

# --- Fake LLM client (canned markdown body), mirroring the Task-5 author tests. ------


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


# --- DB seeding helpers ---------------------------------------------------------------


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


# A PRISTINE counter-0 scaffold, written inline. Deliberately NOT copied from the
# repo's edge/ -- those are LIVING documents whose frontmatter counters advance with
# every merged reflection PR (the first one moved them to 472/506, which silently
# turned every copied-fixture test's premise false and broke CI).
_PRISTINE_MD = """---
forward_closed_at_last_reflection: 0
last_reflected: null
---

> Maintained by the reflection pass; hand-editable; changes land as a human-gated PR.

## Thesis

{pt} seed thesis.

## Confirmed edges

_none yet_

## Screened candidates

_none yet_

## Hunches / needs a test

_none yet_

## Falsified / retired

_none yet_

## Open questions

_none yet_
"""


def _seed_edge_dir(tmp_path: Path) -> Path:
    """A pristine (counter-0) edge dir a test can rewrite safely, independent of repo state."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    for pt in ("continuation", "reversal"):
        (edge_dir / f"{pt}.md").write_text(_PRISTINE_MD.format(pt=pt), encoding="utf-8")
    return edge_dir


# --- A tiny synthetic 1d replay frame (a steady uptrend, shallow dips -> continuation
# setups), adapted from test_replay_no_lookahead's _synth so the screened tier has data.
# ``phase`` shifts the dip cycle so a multi-ticker universe isn't 8 copies of one series.
def _synth(n=400, phase=0.0) -> pd.DataFrame:
    idx = pd.bdate_range("2022-01-01", periods=n)
    t = np.arange(n)
    close = 50 + 0.15 * t + 1.2 * np.sin(t / 5.0 + phase)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.05
    low = np.minimum(open_, close) - 0.3
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1e6)}, index=idx)


# A replay universe RICH enough to mint a replay_screened (source=="replay") verdict for
# continuation -- >= 20 closed, >= 8 distinct tickers (the cluster floor), a strongly
# positive corrected bound. A single-ticker fixture grades every cell "none" (thin), which
# makes any per-source stamp assertion vacuous.
def _confirming_replay_frames() -> dict[str, pd.DataFrame]:
    return {f"S{i}": _synth(n=300, phase=0.7 * i) for i in range(8)}


# =====================================================================================
# due_play_types -- the event trigger
# =====================================================================================
def test_due_when_forward_count_advances_by_trigger_from_zero_counter(tmp_path):
    # The seed edge files carry forward_closed_at_last_reflection: 0. Seed EXACTLY
    # _REFLECT_TRIGGER_N closed forward trades for continuation -> due; reversal has 0 -> not.
    edge_dir = _seed_edge_dir(tmp_path)
    with _mem_session() as session:
        _seed(session, [
            _closed_trade(f"T{i}", 1.0, "continuation") for i in range(_REFLECT_TRIGGER_N)
        ])
        due = due_play_types(session, edge_dir=edge_dir)
    assert "continuation" in due
    assert "reversal" not in due


def test_not_due_when_fewer_than_trigger_new(tmp_path):
    edge_dir = _seed_edge_dir(tmp_path)
    with _mem_session() as session:
        _seed(session, [
            _closed_trade(f"T{i}", 1.0, "continuation")
            for i in range(_REFLECT_TRIGGER_N - 1)
        ])
        due = due_play_types(session, edge_dir=edge_dir)
    assert "continuation" not in due


def test_trigger_measures_delta_from_the_recorded_counter(tmp_path):
    # An edge file whose counter is already 20: only >= 20 MORE closed trades (>= 40 total)
    # re-arms the trigger. 39 total (19 new) -> not due.
    edge_dir = _seed_edge_dir(tmp_path)
    (edge_dir / "continuation.md").write_text(
        "---\nforward_closed_at_last_reflection: 20\nlast_reflected: null\n---\n\nbody\n",
        encoding="utf-8",
    )
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(39)])
        assert "continuation" not in due_play_types(session, edge_dir=edge_dir)
        _seed(session, [_closed_trade("T39", 1.0, "continuation")])   # now 40 total, 20 new
        assert "continuation" in due_play_types(session, edge_dir=edge_dir)


def test_missing_edge_file_treated_as_zero_counter(tmp_path):
    # No file at all -> counter defaults to 0, so _REFLECT_TRIGGER_N closed trades is due.
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    with _mem_session() as session:
        _seed(session, [
            _closed_trade(f"T{i}", 1.0, "reversal") for i in range(_REFLECT_TRIGGER_N)
        ])
        assert "reversal" in due_play_types(session, edge_dir=edge_dir)


def test_trigger_counts_only_baseline_default_forward_book(tmp_path):
    # Trades on a non-baseline arm or non-default variant are NOT the forward book the
    # trigger watches, so they don't count toward the threshold.
    edge_dir = _seed_edge_dir(tmp_path)
    with _mem_session() as session:
        _seed(session, [
            _closed_trade(f"A{i}", 1.0, "continuation", arm="partial33_cond")
            for i in range(_REFLECT_TRIGGER_N)
        ] + [
            _closed_trade(f"V{i}", 1.0, "continuation", variant="extguard_tight")
            for i in range(_REFLECT_TRIGGER_N)
        ])
        assert "continuation" not in due_play_types(session, edge_dir=edge_dir)


# =====================================================================================
# run_reflection -- grade + author + stamp, only the due play types
# =====================================================================================
def test_run_reflection_rewrites_due_advances_counter_and_leaves_other_untouched(tmp_path):
    edge_dir = _seed_edge_dir(tmp_path)
    reversal_before = (edge_dir / "reversal.md").read_text(encoding="utf-8")

    n = _REFLECT_TRIGGER_N + 5   # continuation is due; reversal has nothing
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        client = _FakeClient("## Thesis\n\nAuthored prose.\n\nSome edge narrative.\n")
        reflected = run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=client, today="2026-06-20",
        )

    assert reflected == ["continuation"]
    # The due file is rewritten and its frontmatter counter advances to the FORWARD count.
    cont_after = (edge_dir / "continuation.md").read_text(encoding="utf-8")
    st = parse_state(cont_after)
    assert st.forward_closed_at_last_reflection == n
    assert st.last_reflected == "2026-06-20"
    assert "Authored prose." in cont_after
    # The non-due file is byte-for-byte untouched.
    assert (edge_dir / "reversal.md").read_text(encoding="utf-8") == reversal_before


def test_run_reflection_refuses_to_grade_a_due_book_against_an_empty_corpus(tmp_path):
    """A due play type with NO replay corpus must FAIL LOUDLY, never silently grade the
    screened tier against nothing and open a PR (reflect.yml's 'never silently no-op'
    DB-guard ethos). The concrete trigger: reflect.yml pins nothing, so the corpus is a
    fresh fetch -- a total fetch failure (network down), or a hypothetical --as-of pin on
    the ephemeral runner's empty cache (the loader is cache-only), yields {} here. Without
    this guard the run would grade every replay bucket empty and open a garbage PR."""
    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N + 5  # continuation is due
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        with pytest.raises(ValueError, match="replay corpus is empty"):
            run_reflection(
                session, replay_frames={}, spy_daily=None,
                edge_dir=edge_dir, client=_FakeClient("## Thesis\n\nx.\n"),
                today="2026-06-20",
            )
    # The due file is left byte-for-byte untouched -- a failed run writes nothing.
    assert parse_state(
        (edge_dir / "continuation.md").read_text(encoding="utf-8")
    ).forward_closed_at_last_reflection == 0


def test_run_reflection_empty_corpus_is_a_clean_noop_when_nothing_is_due(tmp_path):
    """The guard fires only when there is a book to grade AND nothing to grade it against.
    With no due play type there is nothing to screen, so an empty corpus is a clean no-op
    (the early return), never an error -- the guard must not punish a quiet week."""
    edge_dir = _seed_edge_dir(tmp_path)  # counters 0, and no forward trades seeded -> none due
    with _mem_session() as session:
        reflected = run_reflection(
            session, replay_frames={}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("## Thesis\n\nx.\n"), today="2026-06-20",
        )
    assert reflected == []


def test_run_reflection_preserves_prior_thesis(tmp_path):
    # A hand-edited thesis in the prior file must survive a reflection (the author seam is
    # handed the prior thesis, and the fallback render carries it too).
    edge_dir = _seed_edge_dir(tmp_path)
    custom = "My hand-written continuation thesis that must survive."
    (edge_dir / "continuation.md").write_text(
        "---\nforward_closed_at_last_reflection: 0\nlast_reflected: null\n---\n\n"
        f"## Thesis\n\n{custom}\n\n## Confirmed edges\n\n_none yet_\n",
        encoding="utf-8",
    )
    n = _REFLECT_TRIGGER_N
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        # A blank LLM reply forces the deterministic fallback, which echoes the thesis.
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("  "), today=None,
        )
    assert custom in (edge_dir / "continuation.md").read_text(encoding="utf-8")


def test_run_reflection_uses_default_thesis_when_file_missing(tmp_path):
    # Missing file -> the per-play-type default thesis is used (and matches _DEFAULT_THESIS).
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    n = _REFLECT_TRIGGER_N
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "reversal") for i in range(n)])
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient(""), today=None,
        )
    out = (edge_dir / "reversal.md").read_text(encoding="utf-8")
    assert _DEFAULT_THESIS["reversal"] in out


def test_run_reflection_nothing_due_writes_nothing(tmp_path):
    edge_dir = _seed_edge_dir(tmp_path)
    before = {pt: (edge_dir / f"{pt}.md").read_text(encoding="utf-8")
              for pt in ("continuation", "reversal")}
    with _mem_session() as session:
        # Below threshold -> nothing due.
        _seed(session, [_closed_trade("T0", 1.0, "continuation")])
        reflected = run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("x"), today=None,
        )
    assert reflected == []
    for pt in ("continuation", "reversal"):
        assert (edge_dir / f"{pt}.md").read_text(encoding="utf-8") == before[pt]


# =====================================================================================
# run_reflection -- the variant-drafting seam is wired in (Task 6)
# =====================================================================================
def test_run_reflection_drafts_a_queued_variant_when_drafter_returns_valid(tmp_path):
    # An injectable drafter that returns a legal NON-indicator delta -> a queued
    # ProposedVariant is written to edge/<pt>.proposed.json for the due play type.
    from swing_screener.pipeline.proposed import QUEUED, load_proposed_for

    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N + 5

    def _drafter(play_type, hunches, base):
        return [{
            "delta": {"max_extension_atr": 1.5},
            "rationale": "tighter freshness gate",
            "hunch_ref": f"{play_type}:hunch",
        }]

    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("## Thesis\n\nprose\n"),
            today="2026-06-20", drafter=_drafter,
        )

    queued = load_proposed_for("continuation", edge_dir)
    assert len(queued) == 1
    assert queued[0].status == QUEUED
    assert queued[0].delta == {"max_extension_atr": 1.5}
    # the non-due play type got no proposed file
    assert not (edge_dir / "reversal.proposed.json").exists()


def test_run_reflection_without_drafter_writes_no_proposed_file(tmp_path):
    # The drafting is ADDITIVE: with no drafter (the default), reflection behaves exactly as
    # before and writes no .proposed.json (existing reflection tests stay green).
    edge_dir = _seed_edge_dir(tmp_path)
    n = _REFLECT_TRIGGER_N
    with _mem_session() as session:
        _seed(session, [_closed_trade(f"T{i}", 1.0, "continuation") for i in range(n)])
        run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=_FakeClient("## Thesis\n\nprose\n"), today="2026-06-20",
        )
    assert not (edge_dir / "continuation.proposed.json").exists()


# =====================================================================================
# main -- thin glue smoke (no network, no real LLM)
# =====================================================================================
def test_main_smoke(monkeypatch, tmp_path):
    import swing_screener.pipeline.reflect as reflect

    edge_dir = _seed_edge_dir(tmp_path)
    captured: dict = {}

    # In-memory DB shared across the StaticPool so the Session main opens sees our seed.
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed(s, [_closed_trade(f"T{i}", 1.0, "continuation")
                  for i in range(_REFLECT_TRIGGER_N)])

    monkeypatch.setattr(reflect, "get_engine", lambda url: engine)
    monkeypatch.setattr(reflect, "fetch_daily", lambda tickers, cache_dir: {"S": _synth()})
    monkeypatch.setattr(reflect, "fetch_bars", lambda *a, **k: None)  # no SPY

    def _fake_run(session, **kw):
        captured.update(kw)
        return ["continuation"]

    monkeypatch.setattr(reflect, "run_reflection", _fake_run)
    monkeypatch.setattr("sys.argv", [
        "reflect", "--tickers", "AMD,NVDA", "--edge-dir", str(edge_dir),
        "--cache-dir", str(tmp_path / ".cache"),
    ])
    reflect.main()
    # The glue wired the replay frames + edge_dir through to run_reflection.
    assert captured["edge_dir"] == edge_dir
    assert "S" in captured["replay_frames"]
    # Unpinned runs STILL thread a corpus stamp -- it records the actual (possibly
    # mixed) cache vintages, which is the honest provenance for an unpinned regen.
    assert captured["corpus_id"].startswith("corpus:")
    assert captured["verdicts_only"] is False


def test_run_reflection_force_reflects_all_play_types_even_when_not_due(tmp_path):
    """force=True is the manual re-grade path for when a graded facet itself changes
    (e.g. the 2026-07-03 score-definition change): it must reflect EVERY play type
    without waiting for the 20-new-closes re-arm."""
    edge_dir = _seed_edge_dir(tmp_path)
    with _mem_session() as session:
        _seed(session, [_closed_trade("T0", 1.0, "continuation")])  # far below the trigger
        assert due_play_types(session, edge_dir=edge_dir) == []
        client = _FakeClient("## Thesis\n\nAuthored prose.\n\nSome edge narrative.\n")
        reflected = run_reflection(
            session, replay_frames={"S": _synth()}, spy_daily=None,
            edge_dir=edge_dir, client=client, today="2026-07-03", force=True,
        )
    assert set(reflected) == {"continuation", "reversal"}


def test_run_reflection_verdicts_only_writes_sidecars_and_nothing_else(tmp_path: Path):
    """--verdicts-only is the stamping-regen path: it grades + rewrites the SIDECARS for
    EVERY play type (the due-gate would no-op a regen) and touches NOTHING else -- the
    Opus-authored md stays byte-identical (a template rewrite would destroy the prose; a
    re-author would burn Opus spend), the drafter is never consulted (a rewrite would
    clobber queued proposals), and the frontmatter counter is untouched."""
    import json

    edge_dir = _seed_edge_dir(tmp_path)
    proposed_before = '[{"hand": "written"}]'
    (edge_dir / "continuation.proposed.json").write_text(proposed_before, encoding="utf-8")
    md_before = {pt: (edge_dir / f"{pt}.md").read_text(encoding="utf-8")
                 for pt in ("continuation", "reversal")}

    def _drafter(play_type, hunches, base):  # would queue a variant if ever consulted
        return [{"delta": {"max_extension_atr": 1.5}, "rationale": "x", "hunch_ref": "h"}]

    with _mem_session() as session:
        _seed(session, [_closed_trade("T0", 1.0, "continuation")])  # far below the trigger
        assert due_play_types(session, edge_dir=edge_dir) == []
        reflected = run_reflection(
            session, replay_frames=_confirming_replay_frames(), spy_daily=None,
            edge_dir=edge_dir,
            client=_FakeClient("MUST NOT APPEAR"), today="2026-07-11", drafter=_drafter,
            corpus_id="corpus: 8/8 tickers · vintage(s) 20260703:8 · pinned as-of 20260703",
            verdicts_only=True,
        )

    # implies force-all-play-types: both reflected despite neither being due.
    assert set(reflected) == {"continuation", "reversal"}
    # NON-VACUOUS by construction: the confirming fixture must mint at least one
    # replay-sourced continuation verdict, or the per-source stamp assertions below
    # never execute and the corpus-id-threads-into-sidecar-rows link is unfalsifiable.
    cont_rows = json.loads(
        (edge_dir / "continuation.verdicts.json").read_text(encoding="utf-8"))
    assert any(r["source"] == "replay" for r in cont_rows)
    for pt in ("continuation", "reversal"):
        # the sidecar is (re)written and carries the threaded corpus id on replay rows...
        rows = json.loads((edge_dir / f"{pt}.verdicts.json").read_text(encoding="utf-8"))
        assert rows
        for r in rows:
            if r["source"] == "replay":
                assert r["cost_level"] == "0.05"
                assert r["corpus_id"].endswith("pinned as-of 20260703")
            elif r["source"] == "none":
                assert r["cost_level"] is None and r["corpus_id"] is None
        # ...while the md (prose + frontmatter counter) is byte-for-byte untouched.
        assert (edge_dir / f"{pt}.md").read_text(encoding="utf-8") == md_before[pt]
    # the queued-proposals store is untouched (drafting skipped entirely).
    assert (edge_dir / "continuation.proposed.json").read_text(
        encoding="utf-8") == proposed_before
    assert not (edge_dir / "reversal.proposed.json").exists()


def _write_vintage_parquet(cache_dir: Path, name: str, rows: int) -> None:
    """A tiny ``<cache>/1d/<TICKER>_<YYYYMMDD>.parquet`` snapshot (the pinning layout)."""
    idx = pd.date_range("2024-01-01", periods=rows, freq="1D")
    df = pd.DataFrame({"open": [1.0] * rows, "high": [2.0] * rows, "low": [0.5] * rows,
                       "close": [1.5] * rows, "volume": [100.0] * rows}, index=idx)
    (cache_dir / "1d").mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_dir / "1d" / f"{name}.parquet")


def test_main_as_of_loads_pinned_cache_only_and_threads_corpus_id(
    monkeypatch, tmp_path: Path, caplog,
):
    """--as-of is the pinned-loading path: frames come from the cached snapshot at/before
    the pin (the OLDER vintage here), NEVER the network, and the corpus stamp naming the
    pin is threaded through to run_reflection as corpus_id."""
    import logging

    import swing_screener.pipeline.reflect as reflect

    cache = tmp_path / ".cache"
    _write_vintage_parquet(cache, "AMD_20260601", rows=3)   # the pinned (older) vintage
    _write_vintage_parquet(cache, "AMD_20260703", rows=5)   # newer -- must NOT be picked
    edge_dir = _seed_edge_dir(tmp_path)

    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(reflect, "get_engine", lambda url: engine)

    def _no_network(*a, **k):
        raise AssertionError("a pinned regen must never fetch from the network")

    monkeypatch.setattr(reflect, "fetch_daily", _no_network)
    monkeypatch.setattr(reflect, "fetch_bars", _no_network)

    captured: dict = {}

    def _fake_run(session, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(reflect, "run_reflection", _fake_run)
    monkeypatch.setattr("sys.argv", [
        "reflect", "--tickers", "AMD,MISSING", "--edge-dir", str(edge_dir),
        "--cache-dir", str(cache), "--as-of", "20260615",
    ])
    with caplog.at_level(logging.WARNING, logger="swing_screener.pipeline.reflect"):
        reflect.main()

    # AMD resolved to the pinned (older, 3-row) vintage; MISSING skipped with a warning.
    assert set(captured["replay_frames"]) == {"AMD"}
    assert len(captured["replay_frames"]["AMD"]) == 3
    # No cached SPY at the pin -> None (never a fetch), and the degradation is LOUD:
    # regime goes unstamped for the whole regen, unlike a single skipped ticker.
    assert captured["spy_daily"] is None
    assert any("no cached SPY" in r.message for r in caplog.records)
    # the corpus stamp records the pin and is threaded as corpus_id.
    assert "pinned as-of 20260615" in captured["corpus_id"]
    assert "1/2 tickers" in captured["corpus_id"]


def test_main_verdicts_only_withholds_the_drafter(monkeypatch, tmp_path: Path):
    """--verdicts-only reaches run_reflection AND main withholds the drafter outright
    (belt-and-braces on top of the in-run carve, which already skips drafting)."""
    import swing_screener.pipeline.reflect as reflect

    edge_dir = _seed_edge_dir(tmp_path)
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(reflect, "get_engine", lambda url: engine)
    monkeypatch.setattr(reflect, "fetch_daily", lambda tickers, cache_dir: {"S": _synth()})
    monkeypatch.setattr(reflect, "fetch_bars", lambda *a, **k: None)  # no SPY

    captured: dict = {}

    def _fake_run(session, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(reflect, "run_reflection", _fake_run)
    monkeypatch.setattr("sys.argv", [
        "reflect", "--tickers", "S", "--edge-dir", str(edge_dir),
        "--cache-dir", str(tmp_path / ".cache"), "--verdicts-only",
    ])
    reflect.main()
    assert captured["verdicts_only"] is True
    assert captured["drafter"] is None


def test_reflection_grades_only_the_would_surface_facet(tmp_path):
    """North Star #7: verdicts are earned on the trades the digest would SURFACE. A fat
    edge living entirely in hidden rows (would_surface False -- or legacy None) must not
    mint a forward-sourced verdict; the identical edge on surfaced rows must."""
    import json

    for flag, expect_forward in ((False, False), (None, False), (True, True)):
        edge_dir = tmp_path / f"edge_{flag}"
        edge_dir.mkdir()
        with _mem_session() as session:
            trades = [_closed_trade(f"T{i}", 1.0, "reversal")
                      for i in range(_REFLECT_TRIGGER_N + 5)]
            for t in trades:
                t.would_surface = flag
            _seed(session, trades)
            client = _FakeClient("## Thesis\n\nAuthored prose.\n")
            run_reflection(session, replay_frames={"S": _synth()}, spy_daily=None,
                           edge_dir=edge_dir, client=client, today="2026-07-03")
        verdicts = json.loads(
            (edge_dir / "reversal.verdicts.json").read_text(encoding="utf-8"))
        has_forward = any(v["source"] == "forward" for v in verdicts)
        assert has_forward is expect_forward, f"would_surface={flag}"
