"""The Strategy Board's evidence ranking: ``GET /api/strategies`` (Task 23).

The load-bearing properties proven here:

* THE RANKING LAW: tier DESC (``forward_confirmed`` > ``replay_screened`` >
  ``hunch``), then the CI floor DESC, nulls LAST. Tier dominates the floor --
  a hunch with a fatter lower bound can never outrank a screened edge, which
  is the whole point of ranking on evidence rather than on the number.
* UNKNOWN IS NEVER GREEN: a missing edge dir / sidecar yields ``tier: null``
  and ``best_cohort: null`` -- never a fabricated ``hunch``, which would read
  as "graded, and it's a hunch" instead of "never graded".
* NO REIMPLEMENTED GATE ARITHMETIC: ``calibration`` / ``gate_ready`` are
  ``pipeline.autonomy``'s own report, field for field, and the countdown line is
  the verbatim string ``gate_countdown`` builds (whose format
  tests/pipeline/test_autonomy_countdown.py pins).
* PEEK ONLY: the endpoint is the READ half of the Strategy Board -- a virgin
  ``agent_guardrails`` table under a read-only DB grant still answers 200 with the
  honest default, with NOT ONE write attempted (the G7 pin).

All sqlite + tmp edge dirs -- no venue, no network, no LLM.
"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import _CLUSTER_FLOOR, MIN_LEADERBOARD_N
from swing_screener.db import guardrails_repo as gr
from swing_screener.db.models import AgentGuardrails
from swing_screener.pipeline.autonomy import autonomy_gate, gate_countdown
from swing_screener.pipeline.proposed import PLAY_TYPES
from swing_screener.pipeline.reflect import Verdict, verdicts_filename, verdicts_to_json
from tests.cockpit.conftest import (
    STAT_KEYS,
    _book_trade,
    _client_and_engine,
    _deny_writes,
    _grade_call,
)

TOP_KEYS = {"strategies", "effective_scope", "ceiling", "disabled", "as_of", "ci_note"}
ROW_KEYS = {"play_type", "rank", "in_ceiling", "disabled", "effective",
            "playbook_present", "tier", "best_cohort", "calibration", "gate_ready",
            "edge_confirmed", "forward"}
COHORT_KEYS = {"dimension", "bucket", "expectancy", "ci_low", "n"}
CALIB_KEYS = {"countdown", "calibrated", "n_high", "n_low", "n_clusters_high",
              "n_clusters_low", "min_per_bucket", "cluster_floor", "ci_low",
              "high_minus_low", "reason"}
FORWARD_KEYS = {"stat", "source"}


def _verdict(**over: object) -> Verdict:
    base: dict = dict(
        play_type="reversal", dimension="market_trend", bucket="bear",
        tier="replay_screened", n=2303, expectancy_r=0.237, ci_low=0.14,
        n_clusters=100, source="replay",
    )
    base.update(over)
    return Verdict(**base)


def _write_playbook(edge_dir: Path, play_type: str, verdicts: list[Verdict]) -> None:
    """Both halves of a playbook: the prose md and the code-owned sidecar. The
    endpoint's ``playbook_present`` needs BOTH, so tests that want a half-present
    book write only one of them."""
    (edge_dir / f"{play_type}.md").write_text("# playbook\n", encoding="utf-8")
    (edge_dir / verdicts_filename(play_type)).write_text(
        verdicts_to_json(verdicts), encoding="utf-8")


def _rows(client: TestClient) -> dict[str, dict]:
    body = client.get("/api/strategies").json()
    assert set(body) == TOP_KEYS
    return {r["play_type"]: r for r in body["strategies"]}


# --- the ranking law ------------------------------------------------------------------


def test_ranks_tier_first_then_ci_floor(tmp_path: Path) -> None:
    """Tier DESC then ci_low DESC. The continuation hunch carries the FATTER lower
    bound (0.30 vs 0.14) and still ranks second: the board ranks evidence, not the
    biggest number, so a screened edge always outranks a hunch."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [_verdict(tier="replay_screened", ci_low=0.14)])
    _write_playbook(tmp_path, "continuation",
                    [_verdict(play_type="continuation", tier="hunch", ci_low=0.30)])

    body = client.get("/api/strategies").json()
    order = [(r["play_type"], r["rank"]) for r in body["strategies"]]
    assert order == [("reversal", 1), ("continuation", 2)]
    assert body["strategies"][0]["tier"] == "replay_screened"
    assert body["strategies"][1]["tier"] == "hunch"
    assert set(body["strategies"][0]) == ROW_KEYS


def test_within_a_tier_the_higher_ci_floor_ranks_first(tmp_path: Path) -> None:
    """Same tier on both books -> the CI floor decides, and only the floor (the
    continuation row's FATTER point estimate loses to reversal's higher bound)."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal",
                    [_verdict(tier="replay_screened", ci_low=0.14, expectancy_r=0.20)])
    _write_playbook(tmp_path, "continuation",
                    [_verdict(play_type="continuation", tier="replay_screened",
                              ci_low=0.05, expectancy_r=0.90)])

    body = client.get("/api/strategies").json()
    assert [r["play_type"] for r in body["strategies"]] == ["reversal", "continuation"]


def test_ungraded_play_types_rank_last(tmp_path: Path) -> None:
    """Nulls LAST: a never-reflected book cannot outrank a graded one, whatever
    alphabetical order would say (continuation sorts first by name)."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [_verdict(tier="hunch", ci_low=-0.9)])

    body = client.get("/api/strategies").json()
    assert [r["play_type"] for r in body["strategies"]] == ["reversal", "continuation"]
    assert body["strategies"][1]["tier"] is None


# --- tier + best cohort ---------------------------------------------------------------


def test_best_cohort_is_the_top_ci_low_within_the_BEST_tier(tmp_path: Path) -> None:
    """The cohort is picked INSIDE the winning tier: the hunch's 0.90 bound is the
    fattest number in the sidecar and is deliberately ignored, because the tier it
    sits in has not cleared any bar."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [
        _verdict(dimension="score", bucket="0.00-0.50",
                 tier="replay_screened", ci_low=0.004, n=8399),
        _verdict(dimension="volatility_tier", bucket="high",
                 tier="replay_screened", ci_low=0.101, n=537, expectancy_r=0.251),
        _verdict(dimension="market_trend", bucket="bull", tier="hunch", ci_low=0.90),
    ])

    row = _rows(client)["reversal"]
    assert row["tier"] == "replay_screened"
    assert set(row["best_cohort"]) == COHORT_KEYS
    assert row["best_cohort"]["ci_low"] == pytest.approx(0.101)
    assert row["best_cohort"]["dimension"] == "volatility_tier"
    assert row["best_cohort"]["bucket"] == "high"
    assert row["best_cohort"]["n"] == 537
    assert row["best_cohort"]["expectancy"] == pytest.approx(0.251)
    assert row["playbook_present"] is True


def test_forward_confirmed_outranks_a_screened_cohort_in_the_same_sidecar(
    tmp_path: Path,
) -> None:
    """The gold tier wins its own sidecar even with the thinner bound -- the same
    ladder ``autonomy._confirmed_edges`` reads, so the board and the gate agree."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [
        _verdict(tier="replay_screened", ci_low=0.50, bucket="bear"),
        _verdict(tier="forward_confirmed", ci_low=0.02, bucket="bull", source="forward"),
    ])

    row = _rows(client)["reversal"]
    assert row["tier"] == "forward_confirmed"
    assert row["best_cohort"]["bucket"] == "bull"
    assert row["edge_confirmed"] is True


def test_missing_edge_dir_is_null_never_a_fabricated_hunch(tmp_path: Path) -> None:
    """UNKNOWN NEVER RENDERS GREEN (nor amber): with no edge files at all, every
    evidence field is an explicit null and ``playbook_present`` is False. Defaulting
    the tier to 'hunch' would tell the operator the book was graded and found
    speculative, when in truth it was never graded."""
    client, _engine = _client_and_engine(tmp_path)

    rows = _rows(client)
    assert set(rows) == set(PLAY_TYPES)
    for row in rows.values():
        assert row["tier"] is None
        assert row["best_cohort"] is None
        assert row["playbook_present"] is False
        assert row["edge_confirmed"] is False
        assert row["gate_ready"] is False


def test_md_without_sidecar_is_not_a_present_playbook(tmp_path: Path) -> None:
    """``playbook_present`` needs BOTH halves: prose with no code-owned sidecar is a
    playbook whose numbers nothing can back."""
    client, _engine = _client_and_engine(tmp_path)
    (tmp_path / "reversal.md").write_text("# prose only\n", encoding="utf-8")

    row = _rows(client)["reversal"]
    assert row["playbook_present"] is False
    assert row["tier"] is None


def test_sidecar_without_md_is_not_a_present_playbook(tmp_path: Path) -> None:
    """The converse: a code-owned sidecar with no prose beside it. The NUMBERS are
    real (the sidecar is the honest source, so tier and cohort still render off it)
    but the playbook itself is not present -- ``playbook_present`` is about the pair,
    never about whether there is something to rank."""
    client, _engine = _client_and_engine(tmp_path)
    (tmp_path / verdicts_filename("reversal")).write_text(
        verdicts_to_json([_verdict(tier="replay_screened", ci_low=0.14)]),
        encoding="utf-8")

    row = _rows(client)["reversal"]
    assert row["playbook_present"] is False
    assert row["tier"] == "replay_screened"          # the sidecar still speaks
    assert row["best_cohort"]["ci_low"] == pytest.approx(0.14)


def test_ci_note_is_the_string_playbooks_serves(tmp_path: Path) -> None:
    """The reuse contract, pinned: ``ci_note`` is IMPORTED from the playbooks router,
    not re-worded here. Two surfaces that render the same verdict bound must define
    it with the same sentence, or one of them will eventually describe a plain 95%
    bound where the number is Bonferroni-corrected."""
    client, _engine = _client_and_engine(tmp_path)

    board = client.get("/api/strategies").json()["ci_note"]
    assert board == client.get("/api/playbooks").json()["ci_note"]
    assert "Bonferroni" in board


def test_corrupt_sidecar_degrades_to_nulls(tmp_path: Path) -> None:
    """A corrupt sidecar reads as no evidence (the GATE's own missing/unreadable
    posture, reused verbatim) -- never a 500 on a polled screen. The loud
    ``unreadable`` marker lives on /api/playbooks, where a human is looking."""
    client, _engine = _client_and_engine(tmp_path)
    (tmp_path / "reversal.md").write_text("# playbook\n", encoding="utf-8")
    (tmp_path / verdicts_filename("reversal")).write_text("{not json", encoding="utf-8")

    row = _rows(client)["reversal"]
    assert row["playbook_present"] is True   # both files exist
    assert row["tier"] is None               # but nothing can be read off them
    assert row["best_cohort"] is None


def test_unknown_tier_string_never_outranks_a_real_one(tmp_path: Path) -> None:
    """A hand-mangled tier is not a rung on the ladder: it cannot become the best
    tier, and a sidecar of nothing BUT unknown tiers reads as ungraded."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [
        _verdict(tier="platinum", ci_low=9.9),
        _verdict(tier="hunch", ci_low=0.01, bucket="bull"),
    ])
    _write_playbook(tmp_path, "continuation",
                    [_verdict(play_type="continuation", tier="platinum", ci_low=9.9)])

    rows = _rows(client)
    assert rows["reversal"]["tier"] == "hunch"
    assert rows["reversal"]["best_cohort"]["bucket"] == "bull"
    assert rows["continuation"]["tier"] is None
    assert rows["continuation"]["best_cohort"] is None


def test_integer_bound_ranks_as_the_number_it_is(tmp_path: Path) -> None:
    """JSON has one number type and ``load_verdicts`` does NO coercion, so a
    hand-edited sidecar hands us ``ci_low: 1`` as a Python int. It is a real bound
    and must rank as one: treating it as absent would file a +1.00R floor BEHIND a
    +0.01R one while the row prints 1 beside it -- the wire contradicting the very
    order it was served in, which is worse than either answer alone."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [_verdict(tier="replay_screened", ci_low=1)])
    _write_playbook(tmp_path, "continuation",
                    [_verdict(play_type="continuation", tier="replay_screened",
                              ci_low=0.01)])

    body = client.get("/api/strategies").json()
    assert [r["play_type"] for r in body["strategies"]] == ["reversal", "continuation"]
    assert body["strategies"][0]["best_cohort"]["ci_low"] == 1


def test_non_numeric_bound_is_null_and_ranks_last(tmp_path: Path) -> None:
    """``ci_low: true`` is not a measurement. Python would rank a bool as +1.0 --
    ahead of every honest bound in its tier -- so it takes the SAME null posture a
    non-finite bound takes, on the wire AND in the order. Same rung, so only the
    bound decides: the honest +0.01R floor wins."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal", [_verdict(tier="replay_screened", ci_low=True)])
    _write_playbook(tmp_path, "continuation",
                    [_verdict(play_type="continuation", tier="replay_screened",
                              ci_low=0.01)])

    body = client.get("/api/strategies").json()
    assert [r["play_type"] for r in body["strategies"]] == ["continuation", "reversal"]
    assert body["strategies"][1]["best_cohort"]["ci_low"] is None


def test_non_finite_verdict_bound_serializes_as_null(tmp_path: Path) -> None:
    """JSON has no ``-Infinity``: an empty bucket's bound serves as an explicit null
    (``_finite_or_none``), never a rendered 'inf' that would read as a measurement."""
    client, _engine = _client_and_engine(tmp_path)
    _write_playbook(tmp_path, "reversal",
                    [_verdict(tier="hunch", ci_low=float("-inf"), n=0)])

    row = _rows(client)["reversal"]
    assert row["tier"] == "hunch"
    assert row["best_cohort"]["ci_low"] is None
    assert row["best_cohort"]["n"] == 0


# --- scope: ceiling x cockpit subtraction ---------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "disabled", "expect"),
    [
        # (env ceiling, cockpit-disabled set) -> per-pt (in_ceiling, disabled, effective)
        (None, set(), {"reversal": (None, False, True),
                       "continuation": (None, False, True)}),
        (None, {"continuation"}, {"reversal": (None, False, True),
                                  "continuation": (None, True, False)}),
        ("reversal", set(), {"reversal": (True, False, True),
                             "continuation": (False, False, False)}),
        ("reversal", {"reversal"}, {"reversal": (True, True, False),
                                    "continuation": (False, False, False)}),
        ("reversal,continuation", {"continuation"},
         {"reversal": (True, False, True), "continuation": (True, True, False)}),
    ],
)
def test_scope_flags_reflect_ceiling_and_subtraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ceiling: str | None, disabled: set[str], expect: dict[str, tuple],
) -> None:
    """``effective`` is ``effective_scope_from_state``'s membership -- the SAME
    function execution enforces -- so the board can never show a play type as traded
    that dispatch would drop (or vice versa). ``in_ceiling`` is null (not False) when
    the env expresses no ceiling: "unset" is not "excluded"."""
    if ceiling is None:
        monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    else:
        monkeypatch.setenv("SWING_EXECUTE_PLAY_TYPES", ceiling)
    client, engine = _client_and_engine(tmp_path)
    if disabled:
        with Session(engine) as s:
            gr.set_disabled_play_types(s, disabled=disabled, source="test")

    body = client.get("/api/strategies").json()
    rows = {r["play_type"]: r for r in body["strategies"]}
    for pt, (in_ceiling, is_disabled, effective) in expect.items():
        assert rows[pt]["in_ceiling"] is in_ceiling, pt
        assert rows[pt]["disabled"] is is_disabled, pt
        assert rows[pt]["effective"] is effective, pt

    assert body["disabled"] == sorted(disabled)
    assert body["ceiling"] == (None if ceiling is None
                               else sorted(ceiling.split(",")))
    expected_scope = sorted(pt for pt, e in expect.items() if e[2])
    if ceiling is None and not disabled:
        assert body["effective_scope"] is None   # genuinely unscoped, not materialised
    else:
        assert body["effective_scope"] == expected_scope


def test_unscoped_ceiling_is_null_not_the_whole_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unset knobs read as "no scoping applies" (null), matching
    ``effective_scope_from_state``'s own honest answer -- materialising the
    vocabulary would let a stale PLAY_TYPES silently exclude a future play type."""
    monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    client, _engine = _client_and_engine(tmp_path)

    body = client.get("/api/strategies").json()
    assert body["ceiling"] is None
    assert body["effective_scope"] is None
    assert body["disabled"] == []


# --- calibration + the gate -----------------------------------------------------------


def _seed_scored_calls(engine: Engine, *, play_type: str, n_high: int, n_low: int) -> None:
    with Session(engine) as s:
        for i in range(n_high):
            s.add(_grade_call(f"H{i}", play_type=play_type, final="high",
                              realized_r=1.0, scored_at=date(2026, 7, 12)))
        for i in range(n_low):
            s.add(_grade_call(f"L{i}", play_type=play_type, final="low",
                              realized_r=-0.5, scored_at=date(2026, 7, 12)))
        s.commit()


def test_calibration_is_the_gates_own_report(tmp_path: Path) -> None:
    """Every calibration number is ``pipeline.autonomy``'s, field for field, and the
    countdown is the VERBATIM line ``gate_countdown`` builds (format pinned by
    tests/pipeline/test_autonomy_countdown.py). Nothing here recomputes the gate."""
    client, engine = _client_and_engine(tmp_path)
    _seed_scored_calls(engine, play_type="reversal", n_high=3, n_low=2)

    with Session(engine) as s:
        report = autonomy_gate(s, edge_dir=tmp_path)
    expected_lines = gate_countdown(report).split("\n")

    rows = _rows(client)
    for pt in PLAY_TYPES:
        gate_pt = report.per_play_type[pt]
        calib = gate_pt["calibration"]
        got = rows[pt]["calibration"]
        assert set(got) == CALIB_KEYS
        assert got["n_high"] == calib.n_high
        assert got["n_low"] == calib.n_low
        assert got["n_clusters_high"] == calib.n_clusters_high
        assert got["n_clusters_low"] == calib.n_clusters_low
        assert got["calibrated"] is calib.calibrated
        assert got["reason"] == calib.reason
        assert got["min_per_bucket"] == MIN_LEADERBOARD_N
        assert got["cluster_floor"] == _CLUSTER_FLOOR
        assert got["countdown"] in expected_lines
        assert got["countdown"].startswith(f"{pt}: ")
        assert rows[pt]["gate_ready"] is gate_pt["ready"]
        assert rows[pt]["edge_confirmed"] is gate_pt["edge_confirmed"]

    # 3 high / 2 low against the 20/20/8 floors: the countdown reads as a countdown,
    # and the un-certifiable bound (-inf) serves as an explicit null.
    assert rows["reversal"]["calibration"]["countdown"] == (
        f"reversal: 3/{MIN_LEADERBOARD_N} high, 2/{MIN_LEADERBOARD_N} low, "
        f"2/{_CLUSTER_FLOOR} tickers")
    assert rows["reversal"]["calibration"]["ci_low"] is None
    assert rows["reversal"]["calibration"]["high_minus_low"] == pytest.approx(1.5)


# --- the forward book -----------------------------------------------------------------


def test_forward_stats_are_the_gold_slice_reflection_grades(tmp_path: Path) -> None:
    """``forward`` is the would_surface GOLD facet of the baseline/default forward
    book -- the exact slice ``pipeline.reflect`` grades -- served as a full Stat with
    a ``source`` note. The research-facet row (would_surface False) is excluded
    BEFORE any math, so the panel can never caption a research number as tradeable."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_book_trade("AMD", 1.0, would_surface=True))
        s.add(_book_trade("NVDA", 2.0, would_surface=True))
        s.add(_book_trade("TSLA", -5.0, would_surface=False))   # research only
        s.add(_book_trade("MSFT", -9.0, would_surface=None))    # legacy, unstampable
        s.commit()

    row = _rows(client)["reversal"]
    assert set(row["forward"]) == FORWARD_KEYS
    assert set(row["forward"]["stat"]) == STAT_KEYS
    assert row["forward"]["stat"]["n"] == 2
    assert row["forward"]["stat"]["value"] == pytest.approx(1.5)
    assert row["forward"]["stat"]["facet"] == "gold"
    assert "would_surface" in row["forward"]["source"]
    # an empty book is an honest zero-n Stat, never an absent key
    assert _rows(client)["continuation"]["forward"]["stat"]["n"] == 0


def test_forward_excludes_non_baseline_arms_and_variants(tmp_path: Path) -> None:
    """The other half of what ``source`` promises: the book is PINNED to
    (arm=baseline, variant=default). The exit-arm A/B and the screen-variant grid
    duplicate every fill, so an unpinned read would count the same entry several
    times and inflate n -- the shadow-grid multiplication ``load_closed_paper_trades``
    exists to dedupe. Only the one baseline/default gold row may count."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_book_trade("AMD", 1.0, would_surface=True))
        s.add(_book_trade("AMD", 9.0, arm="trail", would_surface=True))
        s.add(_book_trade("AMD", 9.0, variant="tighter", would_surface=True))
        s.commit()

    stat = _rows(client)["reversal"]["forward"]["stat"]
    assert stat["n"] == 1
    assert stat["value"] == pytest.approx(1.0)
    assert "arm=baseline" in _rows(client)["reversal"]["forward"]["source"]


# --- the G7 pin: read-only, always -----------------------------------------------------


def test_get_never_writes_on_virgin_table(tmp_path: Path) -> None:
    """The board's own poll is PEEK-ONLY: a virgin ``agent_guardrails`` table under a
    read-only DB grant answers 200 with the honest default and attempts NOT ONE
    write. ``effective_execution_scope`` (the enforcement entry) would have SEEDED
    the row here and 503'd the panel -- this endpoint pairs ``peek_guardrails`` with
    the PURE ``effective_scope_from_state`` instead."""
    client, engine = _client_and_engine(tmp_path)
    with _deny_writes("agent_guardrails", "agent_guardrail_events") as attempted:
        r = client.get("/api/strategies")

    assert attempted == []
    assert r.status_code == 200
    body = r.json()
    assert body["disabled"] == []                # the default, not a seeded row
    assert len(body["strategies"]) == len(PLAY_TYPES)
    with Session(engine) as s:
        assert s.query(AgentGuardrails).count() == 0   # still virgin


def test_deny_writes_listener_actually_fires(tmp_path: Path) -> None:
    """The positive control the G7 test needs: "nothing was attempted" passes just as
    happily when the listener never bound. The same helper over the same table DOES
    refuse the board's own WRITE verb (Task 22's scope set), and the refusal is a
    ``SQLAlchemyError`` -- so an endpoint that let it propagate would 503, not 500."""
    client, engine = _client_and_engine(tmp_path)
    client.get("/api/strategies")                # force the app's engine into being

    with _deny_writes("agent_guardrails") as attempted:
        with Session(engine) as s, pytest.raises(OperationalError):
            gr.set_disabled_play_types(s, disabled={"reversal"}, source="test")
    assert attempted and attempted[0].startswith(("insert into", "update"))
    with Session(engine) as s:
        assert s.query(AgentGuardrails).count() == 0   # the refusal held
