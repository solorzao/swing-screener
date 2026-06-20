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


def _seed_edge_dir(tmp_path: Path) -> Path:
    """Copy the two seed edge files into a temp dir so a test can rewrite them safely."""
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir()
    repo_edge = Path(__file__).resolve().parents[2] / "edge"
    for pt in ("continuation", "reversal"):
        (edge_dir / f"{pt}.md").write_text(
            (repo_edge / f"{pt}.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
    return edge_dir


# --- A tiny synthetic 1d replay frame (a steady uptrend, shallow dips -> continuation
# setups), adapted from test_replay_no_lookahead's _synth so the screened tier has data.
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
