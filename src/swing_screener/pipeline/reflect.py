"""The deterministic GRADER -- the North Star ("evidence over narrative") in code.

Every verdict is stamped by code, never an LLM: each pre-registered bucket gets a
multiple-comparisons-corrected lower bound on its net-of-cost expectancy, and the
bucket is tiered by which book (the forward shadow book or the replay corpus) clears
that bound. The family of hypotheses is FROZEN below; adding a dimension is a
deliberate, git-visible change that resets the Bonferroni denominator K -- you cannot
quietly widen the search and keep the same confidence.

The grader is PURE: it takes already-loaded trade lists (the forward book via the repo,
the replay corpus via ``pipeline.replay``) and returns ``Verdict`` rows. Falsification of
prior claims against the previous edge file lives in a later task, not here.
"""

import argparse
import json
import logging
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import NamedTuple

import anthropic
import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    SCORE_EDGES,
    _bucket_trades_by_score,
    _clustered_ci_low,
    _score_labels,
    cost_level_for,
    score_stamped,
    summarize,
)
from swing_screener.config import StrategyConfig
from swing_screener.config_secrets import get_secret
from swing_screener.data.fetch import fetch_bars
from swing_screener.db import repo
from swing_screener.db.models import AnalystCall, PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.optimize import fetch_daily
from swing_screener.pipeline.proposed import (
    QUEUED,
    ProposedVariant,
    load_proposed_for,
    proposed_to_json,
    to_config,
)
from swing_screener.pipeline.replay import corpus_stamp, load_cached_daily, replay_book
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import load_settings, resolve_edge_dir

log = logging.getLogger(__name__)

# The per-play-type thesis used ONLY when an edge file is missing (a fresh repo or a deleted
# file). Same WORDING as the seed ``edge/<pt>.md`` thesis (the seed hard-wraps for width, so
# it is not byte-identical -- the meaning is). ``run_reflection`` prefers a prior file's own
# (possibly hand-edited) thesis and only falls back to these.
_DEFAULT_THESIS = {
    "continuation": (
        "Heiken-Ashi pullback-continuation: an established uptrend, a shallow HA pullback, "
        "enter long on the bullish HA flip out of the pullback zone."
    ),
    "reversal": (
        "Oversold Heiken-Ashi reversal: a downtrend washout showing a green-out-of-red HA "
        "flip; enter on a pullback into the bounce, not a chase."
    ),
}

# PRE-REGISTERED univariate family (frozen; adding a dimension is a deliberate git-visible
# change that resets K). Categorical dims enumerate buckets; "score" lists its band labels
# from the shared SCORE_EDGES constant (analytics.performance) -- changing THOSE edges is
# likewise a git-visible family change that resets K.
_FAMILY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market_trend", ("bull", "bear")),
    ("volatility_tier", ("low", "med", "high")),
    ("score", tuple(_score_labels(SCORE_EDGES))),  # the score_bucket band labels
)
_ALPHA = 0.05  # family-wise; one-sided Bonferroni per bucket
_MARGIN_R = 0.0  # net-of-cost edge must clear this (replay is haircut upstream)


def _family_size() -> int:
    """K = total buckets across the family (the Bonferroni denominator)."""
    return sum(len(buckets) for _, buckets in _FAMILY)


@dataclass(frozen=True)
class Verdict:
    """One graded (dimension, bucket) cell for a play type. ``ci_low`` is the
    multiple-comparisons-corrected effective lower bound on whichever ``source`` decided
    the tier; the verdict is emitted even for hunches so the edge file can show what's
    being watched.

    ``cost_level`` / ``corpus_id`` are PROVENANCE stamps applied post-``grade`` by
    ``_stamp_provenance`` (``grade`` itself stays pure and emits the defaults). They
    MUST default to ``None``: ``load_verdicts`` is a strict ``Verdict(**d)`` and the
    committed pre-Phase-3 sidecars lack the keys."""

    play_type: str
    dimension: str  # "market_trend" | "volatility_tier" | "score"
    bucket: str  # e.g. "bull", "high", "0.70-0.80"
    tier: str  # "forward_confirmed" | "replay_screened" | "hunch"
    n: int  # n_closed of the deciding source (or the better of the two)
    expectancy_r: float
    ci_low: float  # MC-corrected effective lower bound on the deciding source
    n_clusters: int
    source: str  # "forward" | "replay" | "none"
    cost_level: str | None = None  # slippage level the deciding book's R provably carries
    corpus_id: str | None = None  # replay corpus stamp (replay-sourced verdicts only)


class _BucketBound(NamedTuple):
    """One bucket's Bonferroni-corrected bound + the sample stats that gate/display it.
    A named tuple so the two adjacent ints (``n_closed``/``n_clusters``) can't be transposed
    by a positional caller."""

    eff_low: float
    n_closed: int
    n_clusters: int
    expectancy_r: float
    thin: bool


def _bucket_bound(trades: list[PaperTrade], k: int) -> _BucketBound:
    """Return the effective lower bound + sample stats for one bucket's trades, at the
    Bonferroni-corrected one-sided level alpha/K. Reuses summarize for the
    point/stderr/cluster-count; computes the corrected iid bound and the corrected
    clustered bound, taking the min (never more optimistic than iid)."""
    s = summarize(trades)
    if s.n_closed == 0:
        return _BucketBound(float("-inf"), 0, 0, 0.0, True)
    alpha_c = _ALPHA / max(k, 1)
    z = statistics.NormalDist().inv_cdf(1.0 - alpha_c)  # one-sided
    iid_corr = s.expectancy_r - z * s.expectancy_stderr
    by_ticker: dict[str, list[float]] = {}
    for t in trades:
        if t.realized_r is not None:
            by_ticker.setdefault(t.ticker, []).append(t.realized_r)
    eff_low, n_clusters, thin = _clustered_ci_low(by_ticker, iid_corr, lower_pct=100.0 * alpha_c)
    return _BucketBound(eff_low, s.n_closed, n_clusters, s.expectancy_r, thin)


def _confirms(eff_low: float, n_closed: int, n_clusters: int, thin: bool) -> bool:
    """The three-part gate every tier above ``hunch`` must clear: the corrected bound beats
    the cost margin, the sample is deep enough to trust, and the clustered bootstrap actually
    ran (>= the distinct-ticker floor, i.e. not thin)."""
    return (
        eff_low > _MARGIN_R
        and n_closed >= MIN_LEADERBOARD_N
        and not thin
        and n_clusters >= _CLUSTER_FLOOR
    )


def _bucketed(trades: list[PaperTrade], dimension: str) -> dict[str, list[PaperTrade]]:
    """Slice ``trades`` into the family's pre-registered buckets for one ``dimension``.
    Categorical dims filter by ``getattr(t, dimension) == bucket``; the "score" dim groups
    via ``_bucket_trades_by_score`` (lower-inclusive / upper-exclusive bands) so the verdict
    bucket names match the calibration table exactly."""
    if dimension == "score":
        return _bucket_trades_by_score(trades, SCORE_EDGES)
    buckets = next(b for d, b in _FAMILY if d == dimension)
    return {b: [t for t in trades if getattr(t, dimension) == b] for b in buckets}


def grade(
    play_type: str,
    forward_trades: list[PaperTrade],
    replay_trades: list[PaperTrade],
) -> list[Verdict]:
    """Grade every pre-registered (dimension, bucket) cell for ``play_type``.

    For each cell, slice the matching trades from BOTH books and compute the
    Bonferroni-corrected lower bound (``k = _family_size()``). The tier is:
      * ``forward_confirmed`` if the FORWARD bound clears the three-part gate (source forward),
      * else ``replay_screened`` if the REPLAY bound clears the SAME gate (source replay),
      * else ``hunch`` (source none), carrying whichever book has data for display.
    One verdict per cell -- including empty/hunch cells -- so the edge file can show the
    full watchlist. Deterministic: no LLM, seeded clustered bootstrap.
    """
    k = _family_size()
    verdicts: list[Verdict] = []
    for dimension, buckets in _FAMILY:
        # The score dimension only trusts FORWARD rows scored under the current score
        # definition (score v2 shipped 2026-07-03; older rows measure a different
        # quantity -- pooling them poisons the bands). The replay book is exempt: a
        # replay walk scores every row with the current code, so its historical
        # opened_date says nothing about score vintage.
        fwd = score_stamped(forward_trades) if dimension == "score" else forward_trades
        fwd_groups = _bucketed(fwd, dimension)
        rpl_groups = _bucketed(replay_trades, dimension)
        for bucket in buckets:
            fwd = fwd_groups.get(bucket, [])
            rpl = rpl_groups.get(bucket, [])

            f = _bucket_bound(fwd, k)
            r = _bucket_bound(rpl, k)

            if _confirms(f.eff_low, f.n_closed, f.n_clusters, f.thin):
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="forward_confirmed", n=f.n_closed, expectancy_r=f.expectancy_r,
                    ci_low=f.eff_low, n_clusters=f.n_clusters, source="forward",
                ))
            elif _confirms(r.eff_low, r.n_closed, r.n_clusters, r.thin):
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="replay_screened", n=r.n_closed, expectancy_r=r.expectancy_r,
                    ci_low=r.eff_low, n_clusters=r.n_clusters, source="replay",
                ))
            else:
                # Hunch: carry the richer book for display (forward if it has any closed
                # trades, else replay, else an empty placeholder), but stamp source "none"
                # -- nothing was confirmed.
                if f.n_closed > 0:
                    disp = f
                elif r.n_closed > 0:
                    disp = r
                else:
                    disp = _BucketBound(0.0, 0, 0, 0.0, True)
                verdicts.append(Verdict(
                    play_type=play_type, dimension=dimension, bucket=bucket,
                    tier="hunch", n=disp.n_closed, expectancy_r=disp.expectancy_r,
                    ci_low=disp.eff_low, n_clusters=disp.n_clusters, source="none",
                ))
    return verdicts


# ===========================================================================
# SERIALIZE -- the machine-readable verdicts sidecar.
#
# Phase 2's per-pick insight engine derives a deterministic baseline conviction from these
# verdicts; it MUST read a code-owned artifact, never parse the LLM-authored ``edge/<pt>.md``
# prose (which the model rewrites). ``run_reflection`` therefore also emits
# ``edge/<pt>.verdicts.json`` -- a LOSSLESS round-trip of exactly the ``Verdict`` rows
# ``grade`` produced (provenance-stamped post-grade by ``_stamp_provenance``), written
# deterministically regardless of whether the LLM authoring succeeded. (``Verdict`` is a
# flat frozen dataclass of JSON-native scalars, so ``asdict`` / ``Verdict(**d)``
# round-trips every field; the Phase-3 stamps default so pre-stamp sidecars still load.)
# ===========================================================================


def verdicts_to_json(verdicts: list[Verdict]) -> str:
    """Serialize graded ``verdicts`` to the machine-readable sidecar JSON (lossless)."""
    return json.dumps([asdict(v) for v in verdicts], indent=2)


def load_verdicts(text: str) -> list[Verdict]:
    """Inverse of ``verdicts_to_json``: parse the sidecar JSON back into ``Verdict`` rows."""
    return [Verdict(**d) for d in json.loads(text)]


# ===========================================================================
# RENDER / PARSE -- the pure (no-I/O) edge-file template.
#
# ``render_edge_file`` turns graded verdicts into the markdown playbook; it is ALSO the
# deterministic fallback the Opus authoring seam (Task 5) degrades to on any failure, so it
# must stay pure and produce a complete, parseable file on its own. ``parse_state`` reads
# the event-trigger counter back out of the frontmatter (no yaml dependency: the header is
# the lines between the first two ``---`` fences, ``key: value`` one per line).
# ===========================================================================

# The honesty caveat appended to EVERY quantitative line (North Star: never a bare base
# rate). Centralized so confirmed and screened lines carry identical cost language.
_COST_CAVEAT = "net of cost (optimistic-fill haircut applied)"
# The disclaimer that marks a replay-screened line as NOT proven on the live book. Its
# presence under "Screened candidates" (and absence under "Confirmed edges") is what makes
# gold vs candidate unmistakable to the reader.
_SCREEN_DISCLAIMER = "backtest screen — NOT live-confirmed"
_NONE_PLACEHOLDER = "_none yet_"
# The section header for the code-owned analyst-calibration note (Task 6, Part D). The
# learning loop's report card: does the analyst's judgment prove out on the scored book?
_CALIBRATION_HEADER = "Analyst calibration"
# Conviction grades, best-first, for a stable calibration-note ordering.
_CALIBRATION_ORDER = ("high", "medium", "low", "avoid")


@dataclass(frozen=True)
class ReflectState:
    """The event-trigger state carried in an edge file's frontmatter. ``last_reflected`` is
    the ISO date string of the last reflection (``None`` on a never-reflected seed file)."""

    forward_closed_at_last_reflection: int
    last_reflected: str | None


def _frontmatter_lines(text: str) -> list[str]:
    """The raw ``key: value`` lines between the first two ``---`` fences (empty if absent)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return body
        body.append(line)
    return []  # no closing fence -> treat as no frontmatter


def parse_state(text: str) -> ReflectState:
    """Read the reflection state from an edge file's frontmatter. Tolerant of missing keys
    (counter defaults to 0, last_reflected to None) so a partially hand-edited header still
    parses; the literal ``null`` (or an absent key) maps to ``None``."""
    counter = 0
    last_reflected: str | None = None
    for line in _frontmatter_lines(text):
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), raw.strip()
        if key == "forward_closed_at_last_reflection":
            try:
                counter = int(value)
            except ValueError:
                counter = 0
        elif key == "last_reflected":
            last_reflected = None if value in ("", "null", "~") else value
    return ReflectState(
        forward_closed_at_last_reflection=counter, last_reflected=last_reflected,
    )


def _frontmatter_block(state: ReflectState) -> str:
    """The authoritative ``---`` frontmatter block for an edge file.

    The SINGLE writer of the event-trigger state, so the deterministic render and the Opus
    authoring seam stamp byte-identical headers. ``last_reflected`` serializes to the literal
    ``null`` when unset (which ``parse_state`` maps back to ``None``)."""
    last = state.last_reflected if state.last_reflected else "null"
    return (
        "---\n"
        f"forward_closed_at_last_reflection: {state.forward_closed_at_last_reflection}\n"
        f"last_reflected: {last}\n"
        "---"
    )


def _strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` frontmatter block from ``text``, returning just the body.

    Used to take the Opus-authored markdown and discard any header the model emitted, so the
    code (not the model) can prepend the authoritative frontmatter. Text without a leading
    frontmatter block is returned unchanged (minus a single leading blank line)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[i + 1:])
                return body.lstrip("\n")
    return text.lstrip("\n")


def _with_frontmatter(body: str, state: ReflectState) -> str:
    """Prepend the authoritative frontmatter to a markdown ``body`` (stripping any header the
    body already carried). Code -- never the model -- owns the event-trigger state."""
    return f"{_frontmatter_block(state)}\n\n{_strip_frontmatter(body).rstrip()}\n"


def _section_body(text: str, header: str) -> str:
    """Extract a section's body (the lines after ``## header`` up to the next ``## `` header),
    with the italic intro line and the ``_none yet_`` placeholder dropped. Used to carry the
    prior file's Falsified / retired items forward into the deterministic fallback."""
    marker = f"## {header}"
    start = text.find(marker)
    if start == -1:
        return ""
    rest = text[start + len(marker):]
    nxt = rest.find("\n## ")
    block = rest if nxt == -1 else rest[:nxt]
    kept = [
        ln for ln in block.splitlines()
        if ln.strip() and not (ln.startswith("_") and ln.rstrip().endswith("_"))
    ]
    return "\n".join(kept).strip()


def _condition(v: Verdict) -> str:
    """The human-readable condition for a verdict line, e.g. ``market_trend=bull``."""
    return f"{v.dimension}={v.bucket}"


def _stats_suffix(v: Verdict) -> str:
    """The honesty trio shared by confirmed + screened lines: point estimate, n, the
    clustered 95% CI lower bound, and the net-of-cost caveat. Never a bare base rate."""
    return (
        f"expectancy {v.expectancy_r:+.2f}R, n={v.n} ({v.n_clusters} tickers), "
        f"clustered 95% CI lower bound {v.ci_low:+.2f}R, {_COST_CAVEAT}"
    )


def _verdict_sort_key(v: Verdict) -> tuple[str, str]:
    """Deterministic ordering within a tier: by dimension then bucket."""
    return (v.dimension, v.bucket)


def _confirmed_line(v: Verdict) -> str:
    return f"- **{_condition(v)}** (forward-confirmed): {_stats_suffix(v)}."


def _screened_line(v: Verdict) -> str:
    return f"- **{_condition(v)}** ({_SCREEN_DISCLAIMER}): {_stats_suffix(v)}."


def _hunch_line(v: Verdict) -> str:
    return f"- {_condition(v)}: {_stats_suffix(v)}."


def _section(header: str, intro: str, body: str) -> str:
    """One ``## header`` block with an italic intro and a body (or the none-placeholder)."""
    content = body if body.strip() else _NONE_PLACEHOLDER
    return f"## {header}\n\n_{intro}_\n\n{content}\n"


# The italic intro for the code-owned calibration section. Shared by ``render_edge_file`` (via
# ``_section``) and the post-author re-stitch so BOTH paths emit a byte-identical section.
_CALIBRATION_INTRO = (
    "Code-owned report card on the analyst's conviction calls (scored shadow-book "
    "outcomes): does its judgment prove out?"
)


def _restitch_calibration(text: str, calibration_note: str) -> str:
    """Force the ``## Analyst calibration`` section to the code-owned ``calibration_note``.

    The calibration numbers are facts the LLM is NEVER trusted to grade (like the frontmatter
    counter and the ``verdicts.json`` sidecar). After the model authors the body, CODE replaces
    that section -- whatever the model wrote -- with the deterministic, code-rendered note, so a
    forgetful/lying author can neither drop nor fabricate the numbers. If the model omitted the
    section entirely, it is inserted in its canonical position (before ``## Open questions`` when
    present, else appended). The emitted block is byte-identical to ``render_edge_file``'s, so
    the LLM-success and deterministic-fallback paths produce the same note for the same inputs.
    """
    block = _section(_CALIBRATION_HEADER, _CALIBRATION_INTRO, calibration_note)
    marker = f"## {_CALIBRATION_HEADER}"
    start = text.find(marker)
    if start != -1:
        rest = text[start + len(marker):]
        nxt = rest.find("\n## ")
        end = len(text) if nxt == -1 else start + len(marker) + nxt + 1  # keep the next "## "
        return text[:start] + block + text[end:]
    # Section absent: insert before "## Open questions" if present, else append.
    open_q = text.find("## Open questions")
    if open_q != -1:
        return text[:open_q] + block + "\n" + text[open_q:]
    return text.rstrip("\n") + "\n\n" + block


# ===========================================================================
# ANALYST CALIBRATION -- the code-owned "is the analyst proving out?" note (Part D).
#
# Pure, deterministic, and computed from the SCORED ``AnalystCall`` rows -- never an LLM
# opinion. ``analyst_calibration`` summarizes one play type's scored calls; the reflection
# passes the rendered note into the edge file the same way it passes verdicts, so the
# playbook records whether the analyst's judgment (its conviction grades + its nudges) is
# actually earning R on the live shadow book.
# ===========================================================================


def analyst_calibration(calls: list[AnalystCall]) -> dict:
    """Summarize a play type's SCORED analyst calls (pure; ignores unscored rows).

    Returns ``{"by_conviction": {grade: (n, mean_r)}, "nudge_vs_baseline_r": (n, mean_r)
    | None}``: per FINAL conviction grade, how many scored calls and their mean realized
    R (so you can see if ``high`` out-earns ``low``); and across the NUDGED calls (final !=
    baseline) the count + mean R (so you can see if the analyst's moves add R vs. simply
    keeping the baseline). ``nudge_vs_baseline_r`` is None when no scored call was nudged.
    """
    scored = [c for c in calls if c.scored_at is not None and c.realized_r is not None]
    by_conviction: dict[str, tuple[int, float]] = {}
    for grade in _CALIBRATION_ORDER:
        rs = [c.realized_r for c in scored
              if c.final_conviction == grade and c.realized_r is not None]
        if rs:
            by_conviction[grade] = (len(rs), sum(rs) / len(rs))
    nudged = [c.realized_r for c in scored
              if c.final_conviction != c.baseline_conviction and c.realized_r is not None]
    nudge = (len(nudged), sum(nudged) / len(nudged)) if nudged else None
    return {"by_conviction": by_conviction, "nudge_vs_baseline_r": nudge}


def render_calibration_note(calib: dict) -> str:
    """Render an ``analyst_calibration`` summary into the deterministic note body.

    One line per FINAL conviction grade (n + mean realized R) plus a nudge line (how the
    analyst's nudges fared vs. the baseline). A play type with no scored calls yet renders
    a clear placeholder so the section is always present and self-explanatory."""
    by_conviction: dict[str, tuple[int, float]] = calib["by_conviction"]
    nudge = calib["nudge_vs_baseline_r"]
    if not by_conviction and nudge is None:
        return "_No scored analyst calls yet -- calibration pending._"
    lines = [
        f"- **{grade}** conviction: mean {mean_r:+.2f}R over n={n} scored call(s)."
        for grade, (n, mean_r) in by_conviction.items()
    ]
    if nudge is not None:
        n, mean_r = nudge
        lines.append(
            f"- Nudges (final != baseline): mean {mean_r:+.2f}R over n={n} nudged call(s)."
        )
    else:
        lines.append("- Nudges (final != baseline): none scored yet.")
    return "\n".join(lines)


def render_edge_file(
    play_type: str,
    thesis: str,
    verdicts: list[Verdict],
    *,
    n_closed_now: int,
    prior_falsified: str = "",
    calibration_note: str = "",
) -> str:
    """Render the markdown playbook for ``play_type`` from graded ``verdicts``.

    PURE (no I/O) and deterministic -- identical inputs yield byte-identical output, and the
    output is independent of the input verdict order (each tier is sorted by dimension then
    bucket). This is the Task-5 template fallback, so it must produce a complete, re-parseable
    file: ``parse_state(render_edge_file(..., n_closed_now=N)).forward_closed_at_last_reflection
    == N``.

    Tier routing makes gold-vs-candidate unmistakable:
      * ``forward_confirmed`` -> "Confirmed edges" (the live-confirmed gold tier),
      * ``replay_screened``   -> "Screened candidates", each tagged a backtest screen NOT
        live-confirmed,
      * ``hunch``             -> "Hunches / needs a test".
    Every quantitative line carries n + the clustered 95% CI lower bound + the net-of-cost
    caveat. ``prior_falsified`` is carried verbatim into "Falsified / retired".
    """
    ordered = sorted(verdicts, key=_verdict_sort_key)
    confirmed = "\n".join(_confirmed_line(v) for v in ordered if v.tier == "forward_confirmed")
    screened = "\n".join(_screened_line(v) for v in ordered if v.tier == "replay_screened")
    hunches = "\n".join(_hunch_line(v) for v in ordered if v.tier == "hunch")

    parts = [
        _frontmatter_block(ReflectState(
            forward_closed_at_last_reflection=n_closed_now, last_reflected=None,
        )),
        "",
        "> This playbook is maintained by the **reflection** pass (`pipeline/reflect.py`):"
        " code deterministically grades each pre-registered condition into a tiered verdict"
        " and an Opus seam authors the prose. Every change lands as a **human-gated PR** --"
        " nothing here is auto-merged. The file is also **hand-editable**.",
        "",
        f"## Thesis\n\n{thesis}\n",
        _section(
            "Confirmed edges",
            "Forward-confirmed (gold): cleared the multiple-comparisons-corrected lower "
            "bound on the live forward shadow book.",
            confirmed,
        ),
        _section(
            "Screened candidates",
            "Replay-screened (candidate): cleared the bound on the haircut replay corpus "
            "only -- a backtest screen, NOT live-confirmed.",
            screened,
        ),
        _section(
            "Hunches / needs a test",
            "Watched conditions that have not cleared the bound on either book -- ideas, "
            "not edges.",
            hunches,
        ),
        _section(
            "Falsified / retired",
            "Prior claims now contradicted by the evidence, kept for the record.",
            prior_falsified,
        ),
        _section(_CALIBRATION_HEADER, _CALIBRATION_INTRO, calibration_note),
        _section("Open questions", "Things to investigate next.", ""),
    ]
    return "\n".join(parts)


# ===========================================================================
# AUTHOR -- the OPTIONAL Opus authoring seam.
#
# North Star: the LLM is the AUTHOR, NEVER the grader. Given the deterministic,
# ground-truth verdicts (rendered as the scaffold below) + the prior edge file, Opus writes
# clearer prose and drafts "needs a test" hypotheses -- but it can NEVER change a tier, a
# number, or which bucket is confirmed/screened/hunch. Two guardrails enforce this:
#   1. CODE owns the event-trigger state: the model's body supplies PROSE only; the
#      frontmatter counter is always stamped by ``_with_frontmatter`` (the model is never
#      trusted to set it).
#   2. On ANY failure (missing key, API error, empty/blank reply) we degrade to the pure
#      ``render_edge_file`` template -- so the nightly pipeline never blocks on the LLM.
# The client is an injectable seam (tests pass a fake; prod constructs via get_secret),
# mirroring ``notify/analysis.py`` exactly.
# ===========================================================================

_AUTHOR_SYSTEM = (
    "You are the AUTHOR of a swing-trading edge playbook, NOT its grader.\n\n"
    "You are given deterministic, ground-truth verdicts (each with a tier, n, expectancy, "
    "and clustered CI lower bound) already computed by a rules engine, rendered below as a "
    "ground-truth scaffold. HARD RULE: You MUST NOT change any tier, any number, or which "
    "bucket is confirmed / screened / hunch. Never invent or alter a statistic. Treat every "
    "tier and figure in the scaffold as immutable ground truth.\n\n"
    "You ONLY: write clear, readable prose for each edge; explain the qualitative WHY behind "
    "it; draft 'needs a test' hypotheses for ideas the verdicts don't yet cover; retire stale "
    "items; and keep (curate) the Falsified / retired section. Carry forward the prior file's "
    "Falsified / retired and Open questions.\n\n"
    "Output the FULL markdown playbook with exactly these sections, in this order: "
    "'## Thesis', '## Confirmed edges' (forward-confirmed gold -- live-confirmed only), "
    "'## Screened candidates' (replay-screened -- a backtest screen, NOT live-confirmed), "
    "'## Hunches / needs a test', '## Falsified / retired', '## Analyst calibration' "
    "(code-owned scored-call report card -- reproduce it VERBATIM, never alter a number), "
    "and '## Open questions'. Every "
    "quantitative line must carry n + the clustered 95% CI lower bound + a net-of-cost "
    "caveat -- never a bare base rate. Do NOT emit a frontmatter header; the surrounding "
    "code owns that."
)


def _author_user_content(
    play_type: str, thesis: str, verdicts: list[Verdict], prior_text: str, n_closed_now: int,
    calibration_note: str = "",
) -> str:
    """The user turn: the thesis, the ground-truth scaffold (the deterministic render handed
    over verbatim so the model SEES the exact tiers/numbers it must preserve), and the prior
    edge file text (so it can carry/curate the Falsified + Open-questions sections). The
    scaffold already embeds the code-owned Analyst-calibration note the model must preserve."""
    scaffold = render_edge_file(
        play_type, thesis, verdicts, n_closed_now=n_closed_now,
        calibration_note=calibration_note,
    )
    return (
        f"Play type: {play_type}\n\n"
        f"Thesis:\n{thesis}\n\n"
        "GROUND-TRUTH verdicts, rendered as the deterministic scaffold you must preserve "
        "exactly (do not change any tier or number):\n"
        "<<<GROUND_TRUTH_SCAFFOLD\n"
        f"{scaffold}\n"
        "GROUND_TRUTH_SCAFFOLD\n\n"
        "Prior edge file (carry forward + curate its Falsified / retired and Open "
        "questions; do not resurrect retired items as edges):\n"
        "<<<PRIOR_EDGE_FILE\n"
        f"{prior_text}\n"
        "PRIOR_EDGE_FILE\n\n"
        "Now write the full markdown playbook (no frontmatter)."
    )


def author_edge_file(
    play_type: str,
    thesis: str,
    verdicts: list[Verdict],
    prior_text: str,
    *,
    n_closed_now: int,
    last_reflected: str | None = None,
    client: anthropic.Anthropic | None = None,
    model: str = "claude-opus-4-8",
    calibration_note: str = "",
) -> str:
    """Author the edge-file markdown via Opus, given the GROUND-TRUTH ``verdicts`` + the
    ``prior_text``. The model writes prose + drafts hypotheses but can NEVER change a tier or
    number; the deterministic scaffold it is handed is the immutable ground truth.

    ``client`` is an injectable seam: tests pass a fake so no network call is made; prod
    constructs ``anthropic.Anthropic`` via ``get_secret``. CODE -- not the model -- owns the
    event-trigger frontmatter: whatever body the model returns, ``_with_frontmatter`` strips
    any header it emitted and stamps the authoritative ``forward_closed_at_last_reflection``
    / ``last_reflected``. On ANY failure (missing key, API error, empty/blank reply) we fall
    back to the pure ``render_edge_file`` template (carrying the prior Falsified items) so the
    pipeline never blocks on the LLM.
    """
    state = ReflectState(
        forward_closed_at_last_reflection=n_closed_now, last_reflected=last_reflected,
    )
    try:
        client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=_AUTHOR_SYSTEM,
            messages=[{
                "role": "user",
                "content": _author_user_content(
                    play_type, thesis, verdicts, prior_text, n_closed_now,
                    calibration_note=calibration_note,
                ),
            }],
        )
        text: str = next(
            (
                getattr(b, "text", "")
                for b in resp.content
                if getattr(b, "type", None) == "text"
            ),
            "",
        )
        if not text.strip():
            raise ValueError("empty model response")
        # CODE owns the facts the model is never trusted to grade: re-stitch the
        # code-rendered Analyst-calibration note over whatever the model wrote (it may have
        # dropped or fabricated the numbers), then strip any header it emitted and stamp the
        # real frontmatter. Same guarantee as the verdicts.json sidecar + frontmatter counter.
        text = _restitch_calibration(text, calibration_note)
        return _with_frontmatter(text, state)
    except Exception:
        log.warning(
            "edge-file authoring failed for %s; using deterministic template",
            play_type, exc_info=True,
        )
        return render_edge_file(
            play_type, thesis, verdicts, n_closed_now=n_closed_now,
            prior_falsified=_section_body(prior_text, "Falsified / retired"),
            calibration_note=calibration_note,
        )


# ===========================================================================
# DRAFT -- the OPTIONAL Opus variant-DRAFTING seam (North Star #9).
#
# On a "needs a test" hunch the author may also DRAFT candidate SCREEN VARIANTS: a small
# NON-indicator ``StrategyConfig`` delta keyed to a hunch, which it QUEUES into the validated
# store (``edge/<pt>.proposed.json``) for the optimizer to sweep. The LLM only PROPOSES --
# code owns the schema, the validation, and the human gate:
#   * ``to_config`` is the HARD gate: an illegal (frozen-indicator) or malformed (unknown-key /
#     wrong-shape) draft is DROPPED with a warning -- never persisted, never swept.
#   * On ANY failure (client error / empty / unparseable) -> draft NOTHING (the store is simply
#     not updated), exactly like ``author_edge_file``'s deterministic fallback.
#   * Promotion stays human-gated: a surviving candidate is only QUEUED; the existing
#     ``propose()`` PR gate (and the human reflection PR) still owns promotion to ``config.py``.
# The drafter is an injectable seam (``DrafterFn``): tests pass a fake (no network); prod uses
# ``_opus_drafter`` (a structured tool-use call), mirroring ``author_edge_file``'s client seam.
# ===========================================================================

# A drafter is given (play_type, hunch verdicts, base config) and returns 0+ raw candidate
# dicts -- each ideally ``{"delta": {...}, "rationale": str, "hunch_ref": str}``. It may RAISE
# (client/parse failure); ``draft_variants`` treats that, an empty list, and a malformed item
# identically: the bad item (or the whole run) yields no queued variant. The dict shape (not a
# typed object) is deliberate -- it is untrusted model output; ``draft_variants`` + ``to_config``
# are what validate it.
DrafterFn = Callable[[str, list[Verdict], StrategyConfig], list[dict]]

# The tool the model fills in to PROPOSE variants (structured output). Code owns this schema;
# the model only supplies values, and ``to_config`` still re-validates every delta.
_DRAFT_TOOL = {
    "name": "propose_screen_variants",
    "description": (
        "Propose 0 or more candidate screen variants to walk-forward test. Each variant is a "
        "SMALL delta of non-indicator StrategyConfig detection/zone/scoring knobs (e.g. "
        "max_extension_atr, min_pullback_bars, oversold_rsi_max), a one-line rationale, and the "
        "hunch it is meant to test. Propose nothing if no hunch warrants a test."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "delta": {
                            "type": "object",
                            "description": (
                                "A flat map of StrategyConfig field -> scalar value. NON-indicator "
                                "fields only (never an EMA/ATR/RSI/MACD/HA period)."
                            ),
                        },
                        "rationale": {"type": "string"},
                        "hunch_ref": {"type": "string"},
                    },
                    "required": ["delta", "rationale", "hunch_ref"],
                },
            },
        },
        "required": ["variants"],
    },
}

_DRAFT_SYSTEM = (
    "You are the AUTHOR of a swing-trading edge playbook. Some pre-registered conditions are "
    "HUNCHES -- watched ideas that have NOT cleared the evidence bound on either book. For a "
    "hunch you believe is worth a controlled test, you may PROPOSE a candidate screen variant: "
    "a SMALL delta of NON-indicator StrategyConfig knobs (detection / zone / scoring -- e.g. "
    "max_extension_atr, min_pullback_bars, oversold_rsi_max). NEVER propose a change to a frozen "
    "indicator period (EMA/ATR/RSI/MACD/HA classification): the shadow book reuses shared frames, "
    "so such a variant is illegal and will be dropped. You ONLY propose; a human + the optimizer's "
    "evidence gate decide whether anything is promoted. Propose nothing if no hunch warrants a "
    "test. Use the propose_screen_variants tool."
)


def _draft_user_content(play_type: str, hunches: list[Verdict]) -> str:
    """The user turn for the drafter: the play type and its hunch conditions (the watched
    ideas the model may propose a test for). Pure -- no numbers the model could regrade."""
    lines = "\n".join(f"- {_condition(h)} ({_stats_suffix(h)})" for h in hunches)
    return (
        f"Play type: {play_type}\n\n"
        "These pre-registered conditions are HUNCHES (not yet edges on either book). Propose "
        "0+ candidate screen variants to test the ones you think warrant a controlled test "
        "(non-indicator knobs only):\n"
        f"{lines}\n"
    )


def _opus_drafter(
    *, client: anthropic.Anthropic | None = None, model: str = "claude-opus-4-8",
) -> DrafterFn:
    """Build the prod drafter: a structured tool-use call that asks Opus to fill in the
    ``propose_screen_variants`` tool. Returns a ``DrafterFn`` closing over the client/model.

    ``client`` is an injectable seam (prod constructs ``anthropic.Anthropic`` via ``get_secret``,
    like ``author_edge_file``). The returned function extracts the ``variants`` array from the
    model's ``tool_use`` block; it may RAISE on any failure (missing key, API error, no tool
    block) -- ``draft_variants`` catches that and drafts nothing.
    """
    def _fn(play_type: str, hunches: list[Verdict], base: StrategyConfig) -> list[dict]:
        c = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        # Build kwargs as a plain dict (like notify/analysis.py): the SDK's create() overloads
        # don't reconcile with the heterogeneous tool/tool_choice literals, and this is the
        # untrusted-output seam to_config re-validates anyway.
        kwargs: dict = {
            "model": model,
            "max_tokens": 2000,
            "system": _DRAFT_SYSTEM,
            "tools": [_DRAFT_TOOL],
            "tool_choice": {"type": "tool", "name": _DRAFT_TOOL["name"]},
            "messages": [
                {"role": "user", "content": _draft_user_content(play_type, hunches)}
            ],
        }
        resp = c.messages.create(**kwargs)
        tool_input = next(
            (
                b.input
                for b in resp.content
                if getattr(b, "type", None) == "tool_use"
                and getattr(b, "name", None) == _DRAFT_TOOL["name"]
            ),
            None,
        )
        if not isinstance(tool_input, dict):
            raise ValueError("no propose_screen_variants tool_use block in response")
        variants = tool_input.get("variants", [])
        if not isinstance(variants, list):
            raise ValueError("propose_screen_variants 'variants' is not a list")
        return variants

    return _fn


def _draft_name(play_type: str, hunch_ref: str, index: int) -> str:
    """A stable, reasonable grid name for a drafted candidate, derived from the play type and
    the hunch it tests. The optimizer NAMESPACES + dedupes grid keys, so this need only be
    readable, not globally unique."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in hunch_ref).strip("_") or "hunch"
    return f"{play_type}_{slug}_{index}_q"


def _candidate(
    raw: dict, *, play_type: str, base: StrategyConfig, today: str, index: int,
) -> ProposedVariant | None:
    """Turn one untrusted raw draft dict into a VALIDATED queued ``ProposedVariant``, or return
    ``None`` (with a warning) if it is malformed, illegal, or a no-op.

    Three gates: (1) structural -- the dict must carry a ``dict`` ``delta`` plus string
    ``rationale`` / ``hunch_ref``; (2) semantic -- ``to_config`` must accept the delta (it
    rejects a frozen-indicator or unknown-key delta); (3) no-op -- the delta must CHANGE the
    config (a delta restating the incumbent defaults would sweep the base arm under a second
    name, burning a slot on a settled question -- the 2026-07-02 ``max_extension_atr=2.0``
    draft). A failure DROPS the candidate; it is never persisted and never reaches the sweep.
    """
    delta = raw.get("delta")
    rationale = raw.get("rationale")
    hunch_ref = raw.get("hunch_ref")
    if not (isinstance(delta, dict) and isinstance(rationale, str) and isinstance(hunch_ref, str)):
        log.warning(
            "dropping malformed drafted variant for %s (raw=%r): "
            "expected dict 'delta' + str 'rationale'/'hunch_ref'",
            play_type, raw,
        )
        return None
    pv = ProposedVariant(
        name=_draft_name(play_type, hunch_ref, index),
        play_type=play_type, delta=delta, rationale=rationale, hunch_ref=hunch_ref,
        status=QUEUED, drafted_at=today, provenance="reflection-opus",
    )
    try:
        cfg = to_config(pv, base)   # THE GATE: drop an illegal/unknown/malformed delta.
    except ValueError:
        log.warning(
            "dropping illegal drafted variant %r for %s (delta %r): failed to_config validation",
            pv.name, play_type, delta, exc_info=True,
        )
        return None
    if cfg == base:
        # THE NO-OP GATE: a delta equal to the incumbent config can never move the book.
        log.warning(
            "dropping no-op drafted variant %r for %s (delta %r): equals the incumbent "
            "StrategyConfig defaults, so sweeping it would re-test the base arm",
            pv.name, play_type, delta,
        )
        return None
    return pv


def draft_variants(
    play_type: str,
    hunches: list[Verdict],
    base: StrategyConfig,
    *,
    edge_dir: Path,
    today: str,
    drafter: DrafterFn,
) -> list[ProposedVariant]:
    """Draft + validate + QUEUE candidate screen variants for one play type's hunches.

    The flow: ask the (injectable, fallible) ``drafter`` for raw candidate dicts; validate each
    via ``_candidate`` (``to_config`` is the hard gate -- illegal/malformed drafts are DROPPED +
    warned); and, IF any survive, MERGE them into ``edge/<pt>.proposed.json``.

    The merge (Phase 3): ONLY the ``QUEUED`` rows are machine-owned, so only THEY are replaced
    by the fresh set (a redraft supersedes stale queued ideas). Every non-queued row -- an
    ``approved``/``withdrawn`` decision the cockpit's ``decide_proposal`` wrote (or the older
    hand-edited kind), a ``draft``, anything -- is PRESERVED verbatim, in its original order,
    ahead of the fresh queued set: the decision is the audit record, and it lives in this file
    until git captures it, so a Sunday reflection redraft must not clobber it. And because
    ``_draft_name`` is deterministic (a durable hunch re-drafts at the same index -> the same
    name), a fresh draft whose name matches a preserved row is DROPPED with a log: decided rows
    win; a withdrawn idea is not silently re-queued.

    Fail-safe like ``author_edge_file``: with no hunches the drafter is not even asked; and on
    ANY failure (drafter raises, returns empty, or every candidate is dropped) the store is left
    UNTOUCHED -- a prior valid queued set survives. Returns the fresh queued variants written
    ([] if nothing was queued).
    """
    if not hunches:
        return []
    try:
        raw = drafter(play_type, hunches, base)
    except Exception:
        log.warning(
            "variant drafting failed for %s; queueing nothing (store untouched)",
            play_type, exc_info=True,
        )
        return []
    valid = [
        pv for i, item in enumerate(raw or [])
        if (pv := _candidate(item, play_type=play_type, base=base, today=today, index=i))
        is not None
    ]
    if not valid:
        return []
    preserved = [pv for pv in load_proposed_for(play_type, edge_dir) if pv.status != QUEUED]
    decided_status = {pv.name: pv.status for pv in preserved}
    fresh: list[ProposedVariant] = []
    for pv in valid:
        if pv.name in decided_status:
            log.info(
                "dropping fresh draft %r for %s: its deterministic name matches a prior "
                "%s row -- decided rows win; a withdrawn idea is not silently re-queued",
                pv.name, play_type, decided_status[pv.name],
            )
            continue
        fresh.append(pv)
    (edge_dir / f"{play_type}.proposed.json").write_text(
        proposed_to_json(preserved + fresh), encoding="utf-8"
    )
    log.info(
        "drafted %d queued variant(s) for %s (%d non-queued row(s) preserved)",
        len(fresh), play_type, len(preserved),
    )
    return fresh


# ===========================================================================
# ORCHESTRATION -- the event trigger, the run, and the CLI.
#
# The reflection is EVENT-driven, not time-driven: a play type is reflected only once its
# live FORWARD book has grown by a meaningful batch of new closed trades since the last
# reflection (the counter the edge file's frontmatter records). The run grades that forward
# book against a haircut 1d replay screen, authors the markdown via the Opus seam, and writes
# the file back -- the ONLY side effect. No DB writes; no level/config changes. The CLI mirrors
# ``propose.main`` (load_settings -> engine -> Session -> cached fetch seams -> run) and the
# workflow PRs the edge/*.md diff for a human to merge.
# ===========================================================================

_PLAY_TYPES = ("continuation", "reversal")
# New closed FORWARD trades (per play type) required to re-arm a reflection. Tied to the
# leaderboard's trust floor so a reflection never fires on a sample too thin to grade.
_REFLECT_TRIGGER_N = MIN_LEADERBOARD_N
# Fixed a-priori microstructure haircut applied to the screened (replay) tier so its
# expectancy is net-of-cost like the forward book's. NOT swept -- a sweep here would turn the
# honesty haircut into a tunable knob and reopen the overfit door.
_REPLAY_HAIRCUT_ATR = 0.05
_EDGE_DIR = Path("edge")


def _stamp_provenance(
    verdicts: list[Verdict],
    forward_gold: list[PaperTrade],
    corpus_id: str | None,
) -> list[Verdict]:
    """Stamp cost/corpus provenance onto graded ``verdicts`` (post-``grade``; the grader
    itself stays pure). Per ``source``:

      * ``"replay"``  -> ``cost_level`` is the fixed a-priori haircut every replay fill
        carries (``str(_REPLAY_HAIRCUT_ATR)`` -- the same ``"0.05"`` glyph the cockpit
        renders, never an ``atr_``-prefixed variant), ``corpus_id`` names the replay
        corpus (``None`` when unknown -- the stamp records reality, it never invents it).
      * ``"forward"`` -> ``cost_level`` is what the GOLD forward cohort provably realized
        (``cost_level_for`` -- ``None`` on a mixed gross/net book), ``corpus_id`` is
        ``None``: a forward book has no replay corpus, so stamping one is a category
        error.
      * ``"none"``    -> BOTH ``None``. A hunch cell may DISPLAY numbers from whichever
        book had data, but nothing was confirmed -- it measured nothing, and stamping a
        non-measurement would claim provenance it does not have. (This deliberately
        refines "follow the displayed book" down to the honest floor.)
    """
    forward_level = cost_level_for(forward_gold)
    stamped: list[Verdict] = []
    for v in verdicts:
        if v.source == "replay":
            stamped.append(replace(
                v, cost_level=str(_REPLAY_HAIRCUT_ATR), corpus_id=corpus_id,
            ))
        elif v.source == "forward":
            stamped.append(replace(v, cost_level=forward_level, corpus_id=None))
        else:
            stamped.append(replace(v, cost_level=None, corpus_id=None))
    return stamped


def _edge_text(edge_dir: Path, play_type: str) -> str:
    """The current edge file's text, or "" if it does not exist."""
    path = edge_dir / f"{play_type}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _thesis_for(prior_text: str, play_type: str) -> str:
    """The play type's thesis: the prior file's ``## Thesis`` body if present (so a
    hand-edited thesis survives), else the shared per-play-type default."""
    body = _section_body(prior_text, "Thesis")
    return body if body else _DEFAULT_THESIS[play_type]


def due_play_types(session: Session, edge_dir: Path = _EDGE_DIR) -> list[str]:
    """The play types whose live FORWARD book has grown enough to re-arm a reflection.

    For each play type, read the counter the edge file's frontmatter recorded at the last
    reflection (``parse_state``; 0 when the file is missing) and the CURRENT count of closed
    FORWARD trades at (arm=BASELINE, variant=DEFAULT_VARIANT) -- the exact facet the grader
    grades. A play type is due iff ``current - last >= _REFLECT_TRIGGER_N``.
    """
    due: list[str] = []
    for pt in _PLAY_TYPES:
        last = parse_state(_edge_text(edge_dir, pt)).forward_closed_at_last_reflection
        current = len(repo.load_closed_paper_trades(
            session, play_type=pt, arm=BASELINE, variant=DEFAULT_VARIANT,
        ))
        if current - last >= _REFLECT_TRIGGER_N:
            due.append(pt)
    return due


def run_reflection(
    session: Session,
    *,
    replay_frames: dict[str, pd.DataFrame],
    spy_daily: pd.DataFrame | None,
    edge_dir: Path = _EDGE_DIR,
    client: anthropic.Anthropic | None = None,
    today: str | None = None,
    drafter: DrafterFn | None = None,
    force: bool = False,
    corpus_id: str | None = None,
    verdicts_only: bool = False,
) -> list[str]:
    """Reflect every DUE play type and rewrite its ``edge/<pt>.md``; return the list reflected.

    ``force=True`` reflects EVERY play type regardless of the 20-new-closes re-arm --
    the manual re-grade path for when the graded facets themselves change (a score
    definition change, a family edit) and waiting weeks for the counter would leave
    stale verdicts steering conviction.

    ``corpus_id`` is the ``replay.corpus_stamp`` line for ``replay_frames``' cache
    vintages; it is stamped (with each verdict's cost level) onto the graded verdicts
    post-``grade`` via ``_stamp_provenance``, so the sidecar names its evidence. The
    stamp resolves from the CACHE: unpinned, a fetch-failed ticker with a stale leftover
    snapshot is counted resolved though it was not graded -- pinned mode has no such
    divergence (loading and stamping both read ``cached_daily_file(as_of)``).

    ``verdicts_only=True`` is the SIDECAR-ONLY regen (e.g. re-stamping provenance onto
    existing verdicts): it implies force-all-play-types (the due-gate would no-op a
    regen) and, per play type, grades + rewrites ``edge/<pt>.verdicts.json`` and NOTHING
    else -- no md authoring (a deterministic-template rewrite would destroy the Opus
    prose; with an API key present a plain ``force`` would instead burn an unnecessary
    Opus re-author -- either way wrong for a stamping regen), no drafting (a rewrite
    would clobber queued proposals), and no frontmatter counter reset (the md is left
    byte-untouched).

    For each due play type: load the live forward book (arm=BASELINE, variant=DEFAULT_VARIANT);
    build the screened tier by replaying ``replay_frames`` on the 1d timeframe with the fixed
    ``_REPLAY_HAIRCUT_ATR`` microstructure haircut and the point-in-time SPY regime stamped
    (``spy_daily``), then keep only this play type's trades; ``grade`` forward-vs-replay; and
    ``author_edge_file`` the markdown (carrying the prior thesis + Falsified items, stamping the
    code-owned FORWARD counter). Writing the file is the ONLY side effect -- no DB writes.

    When a ``drafter`` is supplied (North Star #9), the play type's ``hunch`` verdicts are also
    handed to the Opus variant-drafting seam, which may QUEUE validated candidate screen variants
    into ``edge/<pt>.proposed.json`` for the optimizer to sweep. The drafting is ADDITIVE and
    fail-safe: with no drafter (the default) reflection behaves exactly as before, and any drafter
    failure / illegal draft simply queues nothing (the LLM only proposes; ``to_config`` gates).

    Known Phase-1 approximation: the forward book pools ALL timeframes while the replay screen
    is 1d only; acceptable here (the screened tier is a directional candidate, not gold).
    """
    cfg = replace(StrategyConfig(), fill_slippage_atr=_REPLAY_HAIRCUT_ATR)
    base = StrategyConfig()
    # verdicts_only implies force-all: a sidecar regen re-grades what already exists, so
    # the "enough NEW closes" due-gate would simply no-op it.
    all_types = force or verdicts_only
    due = list(_PLAY_TYPES) if all_types else due_play_types(session, edge_dir=edge_dir)
    if not due:
        return []

    # Replay ONCE over the whole universe (the screened tier is the same corpus for every play
    # type; we just slice it per play type below), with the haircut on both the base + default
    # variant and the regime stamped from SPY.
    replay_all = replay_book(
        replay_frames, timeframe="1d", base_cfg=cfg,
        variants={DEFAULT_VARIANT: cfg}, spy_daily=spy_daily,
    )

    for pt in due:
        forward = repo.load_closed_paper_trades(
            session, play_type=pt, arm=BASELINE, variant=DEFAULT_VARIANT,
        )
        # North Star #7 ("measure only what I'd actually trade"): the GOLD forward facet
        # is the trades the digest would have surfaced (the booking-time would_surface
        # stamp -- strength + top-5 rank under the live surfacing config). The full book
        # (~92% hidden EARLY reversals + rank-6+ names) stays the research facet; grading
        # it as gold promoted edges proven on a population the system does not trade
        # (2026-07 review). Legacy NULL rows are excluded (None is not surfaced), so the
        # gold book starts thin after this ships and verdicts honestly downgrade until
        # stamped history accrues -- expected, documented, and reversible (drop the
        # filter to restore the old behavior).
        forward_gold = [t for t in forward if t.would_surface]
        replay_pt = [t for t in replay_all if t.play_type == pt]
        # grade() stays pure; the provenance stamps (cost level + replay corpus id) are
        # applied post-grade so the sidecar names the evidence behind every tier.
        verdicts = _stamp_provenance(
            grade(pt, forward_gold, replay_pt), forward_gold, corpus_id,
        )

        # Emit the machine-readable sidecar FIRST -- it is deterministic + code-owned, so it
        # is written whether or not the (optional, fallible) LLM authoring below succeeds.
        (edge_dir / f"{pt}.verdicts.json").write_text(
            verdicts_to_json(verdicts), encoding="utf-8"
        )

        if verdicts_only:
            # The sidecar-only carve: everything below this line is the md/drafting write
            # path (authoring, frontmatter counter, proposals) -- a stamping regen must
            # leave all of it byte-untouched.
            log.info("reflected %s (verdicts-only): sidecar rewritten, md/proposals "
                     "untouched", pt)
            continue

        # Code-owned analyst-calibration note: summarize this play type's SCORED calls
        # so the playbook records whether the analyst's judgment is proving out. Like the
        # verdicts, it is deterministic and authored by code, never the LLM.
        calibration_note = render_calibration_note(
            analyst_calibration(repo.load_scored_analyst_calls(session, play_type=pt))
        )

        prior_text = _edge_text(edge_dir, pt)
        thesis = _thesis_for(prior_text, pt)
        content = author_edge_file(
            pt, thesis, verdicts, prior_text,
            n_closed_now=len(forward), last_reflected=today, client=client,
            calibration_note=calibration_note,
        )
        (edge_dir / f"{pt}.md").write_text(content, encoding="utf-8")
        log.info("reflected %s: %d forward closed, %d replay-screened",
                 pt, len(forward), len(replay_pt))

        # North Star #9: on the "needs a test" hunches, let the (optional, fail-safe) drafter
        # QUEUE validated candidate screen variants for the optimizer to sweep. The LLM only
        # proposes; ``to_config`` gates and promotion stays human-gated (queue-only).
        if drafter is not None and today is not None:
            hunches = [v for v in verdicts if v.tier == "hunch"]
            draft_variants(
                pt, hunches, base, edge_dir=edge_dir, today=today, drafter=drafter,
            )
    return due


# Default basket mirrors optimize.yml's; the replay universe the screened tier is built over.
_DEFAULT_TICKERS = "AMD,NVDA,AAPL,MSFT,META,AMZN,GOOGL,TSLA,JPM,XOM,WMT,AVGO"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Event-triggered reflection: grade each due play type's forward book "
                    "against a haircut 1d replay screen and rewrite its edge/*.md (no DB "
                    "writes). A workflow PRs the diff for a human to merge.")
    settings = load_settings()
    parser.add_argument("--tickers", default=_DEFAULT_TICKERS, help="comma-separated")
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    # None -> the shared env-first resolution (SWING_EDGE_DIR), so reflection writes to
    # the SAME directory the digest and gate read instead of a cwd-relative default.
    parser.add_argument("--edge-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true",
                        help="reflect every play type now, ignoring the re-arm counter "
                             "(the manual re-grade path after a graded facet changes, "
                             "e.g. a score-definition change)")
    parser.add_argument("--as-of", default=None, metavar="YYYYMMDD",
                        help="pin the replay corpus to cache snapshots at/before this "
                             "fetch date (reproducible evidence; cache-only -- never "
                             "fetches); default = the live fetch seam (newest, possibly "
                             "mixed vintages -- the corpus stamp records whichever)")
    parser.add_argument("--verdicts-only", action="store_true",
                        help="grade + rewrite the verdicts.json sidecars ONLY (implies "
                             "force-all): no md authoring, no drafting, no frontmatter "
                             "reset -- the stamping-regen path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.as_of is not None:
        # Pinned loading: cache-only (a network fetch would write a NEW vintage and
        # defeat the pin). A ticker with no snapshot at/before the pin is skipped.
        replay_frames: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            df = load_cached_daily(ticker, args.cache_dir, args.as_of)
            if df is None:
                log.warning("no cached daily data for %s at/before %s; skipping",
                            ticker, args.as_of)
                continue
            replay_frames[ticker] = df
        spy_daily = load_cached_daily("SPY", args.cache_dir, args.as_of)
        if spy_daily is None:
            # Louder than a skipped ticker: no SPY degrades the WHOLE regen (regime
            # unstamped -> the market_trend replay dimension grades empty).
            log.warning("no cached SPY at/before %s; regime unstamped for this regen",
                        args.as_of)
    else:
        replay_frames = fetch_daily(tickers, args.cache_dir)
        spy_daily = fetch_bars("SPY", "1d", cache_dir=args.cache_dir)
    if not replay_frames:
        log.warning("no replay data fetched for %s; screened tier will be empty", tickers)
    # The stamp is computed in BOTH modes, AFTER loading: --as-of changes what is LOADED;
    # the stamp resolves from the CACHE. Pinned, that is exactly what was graded (loading
    # and stamping both read cached_daily_file(as_of)). Unpinned it can overclaim:
    # fetch_daily skips a ticker on persistent fetch failure, but the stamp re-resolves
    # from the cache and counts a stale leftover snapshot as resolved though it was not
    # graded -- pinned mode has no such divergence.
    corpus_id = corpus_stamp(args.cache_dir, tickers, args.as_of)
    log.info("%s", corpus_id)

    engine = get_engine(settings.db_url)
    with Session(engine) as session:
        reflected = run_reflection(
            session, replay_frames=replay_frames, spy_daily=spy_daily,
            edge_dir=resolve_edge_dir(args.edge_dir), today=date.today().isoformat(),
            # belt-and-braces; the carve already skips drafting
            drafter=None if args.verdicts_only else _opus_drafter(), force=args.force,
            corpus_id=corpus_id, verdicts_only=args.verdicts_only,
        )
    if reflected:
        log.info("reflected play types: %s", ", ".join(reflected))
    else:
        log.info("no play type due for reflection (forward book has not advanced enough)")


if __name__ == "__main__":
    main()
