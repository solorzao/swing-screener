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

import statistics
from dataclasses import dataclass
from typing import NamedTuple

from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    _bucket_trades_by_score,
    _clustered_ci_low,
    _score_labels,
    summarize,
)
from swing_screener.db.models import PaperTrade

# PRE-REGISTERED univariate family (frozen; adding a dimension is a deliberate git-visible
# change that resets K). Categorical dims enumerate buckets; "score" lists its band labels.
_SCORE_EDGES = (0.5, 0.6, 0.7, 0.8)
_FAMILY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market_trend", ("bull", "bear")),
    ("volatility_tier", ("low", "med", "high")),
    ("score", tuple(_score_labels(_SCORE_EDGES))),  # the score_bucket band labels
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
    being watched."""

    play_type: str
    dimension: str  # "market_trend" | "volatility_tier" | "score"
    bucket: str  # e.g. "bull", "high", "0.70-0.80"
    tier: str  # "forward_confirmed" | "replay_screened" | "hunch"
    n: int  # n_closed of the deciding source (or the better of the two)
    expectancy_r: float
    ci_low: float  # MC-corrected effective lower bound on the deciding source
    n_clusters: int
    source: str  # "forward" | "replay" | "none"


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
        return _bucket_trades_by_score(trades, _SCORE_EDGES)
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
        fwd_groups = _bucketed(forward_trades, dimension)
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


def render_edge_file(
    play_type: str,
    thesis: str,
    verdicts: list[Verdict],
    *,
    n_closed_now: int,
    prior_falsified: str = "",
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
        "---",
        f"forward_closed_at_last_reflection: {n_closed_now}",
        "last_reflected: null",
        "---",
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
        _section("Open questions", "Things to investigate next.", ""),
    ]
    return "\n".join(parts)
