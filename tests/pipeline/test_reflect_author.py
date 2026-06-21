"""Tests for the OPTIONAL Opus authoring seam of ``pipeline.reflect`` (Task 5).

``author_edge_file`` lets Opus write nicer prose + draft "needs a test" hypotheses
GIVEN the deterministic, ground-truth verdicts -- but the LLM is the AUTHOR, NEVER the
grader. Two load-bearing properties:

  * **Code owns the state, not the model.** The frontmatter event-trigger counter is
    ALWAYS code-set; the model's body is used only for prose. We prove this by having the
    fake return a body with a WRONG/missing counter and asserting the result carries the
    code-set one.
  * **Graceful fallback on EVERY failure path** (missing key, API error, empty/blank
    response) -> the deterministic ``render_edge_file`` template, so the pipeline never
    blocks on the LLM.

No network: the Anthropic client is an injectable seam and every test passes a fake,
mirroring ``notify/analysis.py``'s fake-client shape.
"""

from swing_screener.pipeline.reflect import (
    Verdict,
    author_edge_file,
    parse_state,
    render_edge_file,
)


def _v(tier: str, *, dimension: str = "market_trend", bucket: str = "bull",
       n: int = 30, expectancy_r: float = 1.2, ci_low: float = 0.4,
       n_clusters: int = 10, play_type: str = "continuation") -> Verdict:
    source = {"forward_confirmed": "forward", "replay_screened": "replay"}.get(tier, "none")
    return Verdict(
        play_type=play_type, dimension=dimension, bucket=bucket, tier=tier,
        n=n, expectancy_r=expectancy_r, ci_low=ci_low, n_clusters=n_clusters, source=source,
    )


# --- Fake client, mirroring analysis.py's shape (resp.content = [text block]). -------
class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _CapturingClient:
    """A fake whose ``.messages.create(**kw)`` records the kwargs it was called with and
    returns a canned text body. Lets a test inspect the prompt the model received."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.kwargs: dict = {}

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                outer.kwargs = kw
                return _Resp(outer._text)

        return _M()


class _BoomClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                raise RuntimeError("api down")

        return _M()


_THESIS = "A test thesis sentence."


# ---------------------------------------------------------------------------
# Code owns the counter: the model's body is used for PROSE, but the
# frontmatter state is ALWAYS code-set -- even if the body had a wrong one.
# ---------------------------------------------------------------------------
def test_author_uses_model_body_but_code_sets_the_counter():
    # The canned body carries its OWN (wrong) frontmatter counter; the code must
    # strip it and stamp the authoritative one.
    canned = (
        "---\n"
        "forward_closed_at_last_reflection: 999\n"
        "last_reflected: 2020-01-01\n"
        "---\n"
        "# Authored playbook\n\n"
        "Some lovely Opus prose about the edge.\n"
    )
    client = _CapturingClient(canned)
    out = author_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], "prior text",
        n_closed_now=37, last_reflected="2026-06-20", client=client,
    )
    # The model's PROSE is carried through.
    assert "Some lovely Opus prose about the edge." in out
    # But CODE owns the counter: 37, not the model's 999.
    st = parse_state(out)
    assert st.forward_closed_at_last_reflection == 37
    assert st.last_reflected == "2026-06-20"
    # Exactly one frontmatter block (the model's was stripped, not duplicated).
    assert out.count("forward_closed_at_last_reflection") == 1


def test_author_sets_null_last_reflected_when_not_supplied():
    canned = "# body\n\nprose\n"  # no frontmatter at all
    out = author_edge_file(
        "continuation", _THESIS, [], "prior", n_closed_now=5,
        client=_CapturingClient(canned),
    )
    st = parse_state(out)
    assert st.forward_closed_at_last_reflection == 5
    assert st.last_reflected is None
    assert "prose" in out


# ---------------------------------------------------------------------------
# The prompt the model receives must include the GROUND-TRUTH verdicts (the
# rendered scaffold) AND a system rule forbidding changing them.
# ---------------------------------------------------------------------------
def test_prompt_includes_ground_truth_scaffold_and_hard_rule():
    verdicts = [
        _v("forward_confirmed", dimension="market_trend", bucket="bull",
           n=30, ci_low=0.4, n_clusters=10),
        _v("replay_screened", dimension="volatility_tier", bucket="high",
           n=50, ci_low=0.3, n_clusters=12),
    ]
    client = _CapturingClient("# authored\n\nprose\n")
    author_edge_file(
        "continuation", _THESIS, verdicts, "prior text",
        n_closed_now=30, client=client,
    )

    system = client.kwargs["system"]
    messages = client.kwargs["messages"]
    user_content = messages[0]["content"]
    user_text = user_content if isinstance(user_content, str) else str(user_content)

    # The ground-truth scaffold (the deterministic render) is handed to the model so it
    # SEES the exact tiers/numbers it must preserve.
    scaffold = render_edge_file(
        "continuation", _THESIS, verdicts, n_closed_now=30,
    )
    # The load-bearing facts from the scaffold appear in the user turn.
    assert "market_trend=bull" in user_text
    assert "volatility_tier=high" in user_text
    assert "n=30" in user_text and "n=50" in user_text
    # The whole scaffold is handed over verbatim (it is the ground truth).
    assert scaffold in user_text
    # The thesis is in the prompt.
    assert _THESIS in user_text
    # The prior edge file text is handed over so the model can curate Falsified / Open.
    assert "prior text" in user_text

    # The system prompt states the HARD RULE: never change a tier/number/bucket.
    low = system.lower()
    assert "must not" in low or "never" in low
    assert "tier" in low
    assert "ground-truth" in low or "ground truth" in low


# ---------------------------------------------------------------------------
# Graceful fallback on EVERY failure path -> the deterministic template.
# ---------------------------------------------------------------------------
def test_falls_back_to_template_when_api_raises():
    verdicts = [_v("forward_confirmed"), _v("replay_screened", dimension="score",
                                            bucket="0.80-1.00")]
    prior = (
        "---\nforward_closed_at_last_reflection: 9\nlast_reflected: null\n---\n"
        "## Falsified / retired\n\n"
        "_intro_\n\n"
        "- market_trend=bear: claimed +0.5R, now contradicted (retired 2026-05-01).\n\n"
        "## Open questions\n\n_none yet_\n"
    )
    out = author_edge_file(
        "continuation", _THESIS, verdicts, prior, n_closed_now=30, client=_BoomClient(),
    )
    # Falls back to the EXACT deterministic template, carrying the prior Falsified items.
    carried = "- market_trend=bear: claimed +0.5R, now contradicted (retired 2026-05-01)."
    expected = render_edge_file(
        "continuation", _THESIS, verdicts, n_closed_now=30, prior_falsified=carried,
    )
    assert out == expected
    # And the code-set counter is right even on the fallback path.
    assert parse_state(out).forward_closed_at_last_reflection == 30


def test_falls_back_to_template_when_response_blank():
    out = author_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], "prior",
        n_closed_now=12, client=_CapturingClient("   \n  \n"),
    )
    expected = render_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], n_closed_now=12,
    )
    assert out == expected


def test_falls_back_to_template_when_response_empty():
    out = author_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], "prior",
        n_closed_now=8, client=_CapturingClient(""),
    )
    expected = render_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], n_closed_now=8,
    )
    assert out == expected


def test_falls_back_to_template_when_no_client_and_no_key(monkeypatch):
    # No injected client and no API key -> constructing the real client / calling it
    # fails inside the try, so we degrade to the deterministic template (no network).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    out = author_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], "prior", n_closed_now=3,
    )
    expected = render_edge_file(
        "continuation", _THESIS, [_v("forward_confirmed")], n_closed_now=3,
    )
    assert out == expected
