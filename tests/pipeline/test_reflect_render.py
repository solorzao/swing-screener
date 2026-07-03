"""Tests for the pure render/parse half of ``pipeline.reflect`` (Task 4).

These cover the deterministic, no-I/O ``render_edge_file`` (which is ALSO the template
fallback the Opus seam degrades to in Task 5) and the ``parse_state`` frontmatter reader.

The North Star is "honest about uncertainty": a forward-confirmed edge (gold) and a
replay-screened candidate (a backtest screen, NOT live-confirmed) must be UNMISTAKABLY
distinct to the reader, and every quantitative line must carry n + the clustered CI lower
bound + an explicit net-of-cost caveat -- never a bare base rate.
"""

from pathlib import Path

from swing_screener.pipeline.reflect import (
    ReflectState,
    Verdict,
    parse_state,
    render_edge_file,
)

_EDGE_DIR = Path(__file__).resolve().parents[2] / "edge"


def _v(tier: str, *, dimension: str = "market_trend", bucket: str = "bull",
       n: int = 30, expectancy_r: float = 1.2, ci_low: float = 0.4,
       n_clusters: int = 10, play_type: str = "continuation") -> Verdict:
    source = {"forward_confirmed": "forward", "replay_screened": "replay"}.get(tier, "none")
    return Verdict(
        play_type=play_type, dimension=dimension, bucket=bucket, tier=tier,
        n=n, expectancy_r=expectancy_r, ci_low=ci_low, n_clusters=n_clusters, source=source,
    )


# ---------------------------------------------------------------------------
# parse_state: reads the frontmatter counter + last_reflected.
# ---------------------------------------------------------------------------
def test_parse_state_reads_seed_counter_and_null_last_reflected():
    text = (
        "---\n"
        "forward_closed_at_last_reflection: 0\n"
        "last_reflected: null\n"
        "---\n"
        "# anything below\n"
    )
    st = parse_state(text)
    assert isinstance(st, ReflectState)
    assert st.forward_closed_at_last_reflection == 0
    assert st.last_reflected is None


def test_parse_state_reads_nonzero_counter_and_a_date():
    text = (
        "---\n"
        "forward_closed_at_last_reflection: 42\n"
        "last_reflected: 2026-06-20\n"
        "---\n"
    )
    st = parse_state(text)
    assert st.forward_closed_at_last_reflection == 42
    assert st.last_reflected == "2026-06-20"


# ---------------------------------------------------------------------------
# The LIVE playbooks on disk must always parse. They are LIVING documents -- the
# frontmatter counter advances with every merged reflection PR (the first one moved
# it 0 -> 472/506), so asserting a zeroed counter is asserting repo history, not an
# invariant. The invariant is parseability + a well-formed counter/date.
# ---------------------------------------------------------------------------
def test_live_playbooks_always_parse():
    import re

    for name in ("continuation.md", "reversal.md"):
        text = (_EDGE_DIR / name).read_text(encoding="utf-8")
        st = parse_state(text)
        assert st.forward_closed_at_last_reflection >= 0
        assert st.last_reflected is None or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(st.last_reflected))


def test_seed_files_carry_thesis_and_section_headers():
    for name in ("continuation.md", "reversal.md"):
        text = (_EDGE_DIR / name).read_text(encoding="utf-8")
        assert "## Thesis" in text
        assert "## Confirmed edges" in text
        assert "## Screened candidates" in text
        assert "## Hunches / needs a test" in text
        assert "## Falsified / retired" in text
        assert "## Open questions" in text
        # maintained-by-reflection note so a human knows it is hand-editable / PR-gated.
        low = text.lower()
        assert "reflection" in low and "hand-edit" in low


# ---------------------------------------------------------------------------
# render_edge_file: frontmatter counter is set to n_closed_now; thesis carried.
# ---------------------------------------------------------------------------
def test_render_sets_frontmatter_counter_and_includes_thesis():
    thesis = "A test thesis sentence."
    out = render_edge_file("continuation", thesis, [_v("forward_confirmed")], n_closed_now=37)
    st = parse_state(out)
    assert st.forward_closed_at_last_reflection == 37
    assert thesis in out
    assert "## Thesis" in out


def test_render_round_trips_the_counter():
    out = render_edge_file("continuation", "t", [], n_closed_now=37)
    assert parse_state(out).forward_closed_at_last_reflection == 37


# ---------------------------------------------------------------------------
# Tier routing: confirmed -> Confirmed edges; screened -> Screened candidates;
# hunch -> Hunches.
# ---------------------------------------------------------------------------
def _section(text: str, header: str) -> str:
    """The slice of ``text`` from ``header`` up to the next ``## `` header (or EOF)."""
    start = text.index(header)
    rest = text[start + len(header):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def test_confirmed_verdict_lands_under_confirmed_edges_with_n_ci_and_cost_caveat():
    out = render_edge_file(
        "continuation", "t",
        [_v("forward_confirmed", dimension="market_trend", bucket="bull",
            n=30, ci_low=0.4, n_clusters=10)],
        n_closed_now=30,
    )
    sec = _section(out, "## Confirmed edges")
    # the condition is rendered as dimension=bucket
    assert "market_trend=bull" in sec
    # n is shown
    assert "n=30" in sec
    # the clustered CI lower bound is shown (the 0.40 figure)
    assert "0.40" in sec
    assert "ci" in sec.lower() and "95" in sec
    # the explicit net-of-cost caveat with the optimistic-fill haircut language
    low = sec.lower()
    assert "net of cost" in low
    assert "haircut" in low


def test_screened_verdict_is_marked_not_live_confirmed():
    out = render_edge_file(
        "continuation", "t",
        [_v("replay_screened", dimension="volatility_tier", bucket="high",
            n=50, ci_low=0.3, n_clusters=12)],
        n_closed_now=10,
    )
    sec = _section(out, "## Screened candidates")
    assert "volatility_tier=high" in sec
    low = sec.lower()
    assert "backtest screen" in low
    assert "not live-confirmed" in low
    # still carries the honesty trio: n + CI + cost caveat.
    assert "n=50" in sec
    assert "0.30" in sec
    assert "net of cost" in low


def test_hunch_verdict_lands_under_hunches():
    out = render_edge_file(
        "continuation", "t",
        [_v("hunch", dimension="score", bucket="0.80-1.00", n=5, ci_low=-0.2, n_clusters=3)],
        n_closed_now=10,
    )
    sec = _section(out, "## Hunches / needs a test")
    assert "score=0.80-1.00" in sec


# ---------------------------------------------------------------------------
# THE load-bearing test: confirmed (gold) vs screened (candidate) are VISIBLY
# DISTINCT -- a reader can tell which is proven-live and which is only a screen.
# ---------------------------------------------------------------------------
def test_confirmed_and_screened_are_unmistakably_distinct():
    confirmed = _v("forward_confirmed", dimension="market_trend", bucket="bull")
    screened = _v("replay_screened", dimension="volatility_tier", bucket="high")
    out = render_edge_file("continuation", "t", [confirmed, screened], n_closed_now=30)

    conf_sec = _section(out, "## Confirmed edges")
    scr_sec = _section(out, "## Screened candidates")

    # Each verdict appears under its OWN section, not the other's.
    assert "market_trend=bull" in conf_sec
    assert "market_trend=bull" not in scr_sec
    assert "volatility_tier=high" in scr_sec
    assert "volatility_tier=high" not in conf_sec

    # The screened section explicitly disclaims live confirmation; the confirmed one
    # does NOT carry that disclaimer -- so the two are not confusable.
    assert "not live-confirmed" in scr_sec.lower()
    assert "not live-confirmed" not in conf_sec.lower()
    # The confirmed section is the only one that calls the edge live/forward-confirmed.
    assert "forward-confirmed" in conf_sec.lower()


# ---------------------------------------------------------------------------
# prior_falsified is carried VERBATIM into the Falsified / retired section.
# ---------------------------------------------------------------------------
def test_prior_falsified_carried_verbatim():
    prior = "- market_trend=bear: claimed +0.5R, now contradicted (retired 2026-05-01)."
    out = render_edge_file(
        "continuation", "t", [_v("forward_confirmed")],
        n_closed_now=30, prior_falsified=prior,
    )
    sec = _section(out, "## Falsified / retired")
    assert prior in sec


# ---------------------------------------------------------------------------
# Empty sections render a placeholder (never blank) so the layout is stable.
# ---------------------------------------------------------------------------
def test_empty_sections_get_a_placeholder():
    out = render_edge_file("continuation", "t", [], n_closed_now=0)
    assert "_none yet_" in out


# ---------------------------------------------------------------------------
# Determinism: same inputs -> identical output, and ordering is stable
# regardless of input verdict order (sorted by dimension then bucket).
# ---------------------------------------------------------------------------
def test_render_is_deterministic_and_order_independent():
    a = _v("forward_confirmed", dimension="market_trend", bucket="bull")
    b = _v("forward_confirmed", dimension="volatility_tier", bucket="high")
    c = _v("forward_confirmed", dimension="score", bucket="0.80-1.00")
    out1 = render_edge_file("continuation", "t", [a, b, c], n_closed_now=30)
    out2 = render_edge_file("continuation", "t", [c, b, a], n_closed_now=30)
    assert out1 == out2
    # within Confirmed edges, ordered by dimension then bucket: market_trend < score < volatility
    sec = _section(out1, "## Confirmed edges")
    assert sec.index("market_trend=bull") < sec.index("score=0.80-1.00") < \
        sec.index("volatility_tier=high")
