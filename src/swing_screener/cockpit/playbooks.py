"""Playbook drift: does the LLM-authored ``edge/<pt>.md`` still tell the sidecar's story?

The stale-verdicts hazard (2026-07-04 review): the md prose is Opus-authored and
hand-editable, while ``edge/<pt>.verdicts.json`` is the code-owned ground truth --
the two can disagree after a hand edit, a partial regen, or an author drift. The
cockpit renders NUMBERS from the sidecar only; this module answers the remaining
question -- does the PROSE still place each graded condition under the tier the
grader assigned it?

The check is PURE and STRUCTURAL, deliberately: each verdict's ``dimension=bucket``
token must appear somewhere in the md section matching its tier (``## Confirmed
edges`` / ``## Screened candidates`` / ``## Hunches / needs a test``). NEVER a
numeric comparison -- the author legitimately rounds and reformats every figure, so
matching numbers would flag every faithful file. The result is an ADVISORY amber,
never a hard error: hand-edits are legitimate (the edge files are explicitly
hand-editable) and a reflection PR merges the two back together.

Section extraction reuses ``pipeline.reflect._section_body`` -- the reflection's own
parser -- so what counts as "inside the Confirmed section" can never drift between
the writer and this checker.
"""

from dataclasses import dataclass

from swing_screener.pipeline.reflect import Verdict, _condition, _section_body

# Tier -> the md section header that should carry the verdict's condition token.
# Mirrors ``reflect.render_edge_file``'s tier routing exactly (the writer this
# checker audits); a tier outside this map has no legitimate home in the md and
# always reads as drift.
_TIER_SECTIONS: dict[str, str] = {
    "forward_confirmed": "Confirmed edges",
    "replay_screened": "Screened candidates",
    "hunch": "Hunches / needs a test",
}


@dataclass(frozen=True, kw_only=True)
class MissingToken:
    """One drifted condition: the ``dimension=bucket`` token absent from the md
    section its sidecar tier demands, plus that tier (so the UI tooltip can say
    'market_trend=bear should be under Screened candidates')."""

    token: str
    tier: str


@dataclass(frozen=True, kw_only=True)
class DriftReport:
    """The drift lamp's data: ``ok`` iff every sidecar verdict's token sits in its
    tier's section. ``missing`` lists the failures in verdict order (deterministic
    -- the sidecar is written in grading order). An EMPTY verdict list is honestly
    ``ok``: there is nothing the prose could contradict."""

    ok: bool
    missing: tuple[MissingToken, ...]


def playbook_drift(md_text: str, verdicts: list[Verdict]) -> DriftReport:
    """Check ``md_text`` (the authored playbook) against the sidecar ``verdicts``.

    STRUCTURAL only (module docstring): a verdict drifts when its
    ``dimension=bucket`` token does not appear in the md section matching its
    tier -- a token present under the WRONG tier is exactly as missing as one
    absent entirely, because the reader would take the wrong tier's word for it.
    Missing/empty md degrades honestly: every verdict's token is missing from an
    empty section, so a blank file with a non-empty sidecar reads all-amber.
    """
    missing: list[MissingToken] = []
    for v in verdicts:
        header = _TIER_SECTIONS.get(v.tier)
        section = _section_body(md_text, header) if header is not None else ""
        # reflect._condition is the WRITER's own token renderer -- sharing it
        # means the checker can never spell the glyph differently than the md.
        token = _condition(v)
        if token not in section:
            missing.append(MissingToken(token=token, tier=v.tier))
    return DriftReport(ok=not missing, missing=tuple(missing))
