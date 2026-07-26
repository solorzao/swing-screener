"""The Strategy Board's evidence ranking: ``GET /api/strategies`` (Task 23).

One row per play type, RANKED BY EVIDENCE -- tier first (``forward_confirmed`` >
``replay_screened`` > ``hunch``), then the CI lower bound on expectancy, nulls last.
The floor, never the point estimate: ranking on the point estimate is how a book
picks noise, and the corrected lower bound is the number the autonomy gate itself
keys on.

HONESTY POSTURE (North Star #2), and every one of these is a rule, not a habit:

* NOTHING IS RECOMPUTED HERE. The calibration counts + readiness are
  ``pipeline.autonomy``'s own report, field for field, and the countdown line is the
  verbatim string ``gate_countdown`` builds (its format is pinned by
  tests/pipeline/test_autonomy_countdown.py). The verdicts sidecar is read through
  the GATE's own loader, so the board and the gate can never disagree about what a
  play type has proven. The forward stats are ``analytics.performance.summarize``
  over ``settlement.facet_filter``'s gold slice -- the exact composition
  ``pipeline.reflect`` grades. This module contributes NO arithmetic of its own.
* UNKNOWN IS NEVER GREEN. A missing (or unreadable) sidecar serves ``tier: null`` and
  ``best_cohort: null`` -- never a defaulted ``hunch``, which would read as "graded,
  and it's speculative" instead of "never graded". Same for a non-finite bound: JSON
  has no infinity, so it serves as an explicit null (``_finite_or_none``).
* READ-ONLY, AND PROVABLY SO. ``peek_guardrails`` + the PURE
  ``effective_scope_from_state`` -- never the seeding ``effective_execution_scope``,
  which is the ENFORCEMENT entry and would INSERT the brake row on a poll (503-ing
  this panel under a read-only DB grant; the G7 grant is not issued in prod yet).
  DB + files only: no broker call, no quote, no LLM.

BUDGET: two ``load_closed_paper_trades`` + two ``summarize`` bootstraps here, plus
the gate's own two ``load_scored_analyst_calls`` + two calibration bootstraps -- four
seeded clustered bootstraps per request, ~0.2s on the dev box (cf. the ~1.25s
/api/stats/performance already spends at a 60s poll). Revisit if a third play type
lands or the poll interval drops.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import (
    _CLUSTER_FLOOR,
    MIN_LEADERBOARD_N,
    CalibrationVerdict,
)
from swing_screener.analytics.performance import cost_level_for, summarize
from swing_screener.cockpit.common import _finite_or_none, _utc_iso
from swing_screener.cockpit.routers.playbooks import _CI_NOTE
from swing_screener.cockpit.settlement import facet_filter
from swing_screener.cockpit.stats import stat_from_summary
from swing_screener.db import guardrails_repo, repo
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.autonomy import (
    _CONFIRMED_TIER,
    _countdown_line,
    _read_verdicts,
    autonomy_gate,
)
from swing_screener.pipeline.proposed import PLAY_TYPES
from swing_screener.pipeline.reflect import Verdict, verdicts_filename
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.settings import load_settings, resolve_edge_dir

#: The evidence LADDER, and the ONLY ordering the board ranks on. The gold rung is
#: ``autonomy._CONFIRMED_TIER`` itself (not a restated literal) so the top of this
#: ladder and the gate's "is there a proven edge" test can never drift apart. A tier
#: string outside this mapping is NOT a rung: it can never become the best tier, so a
#: hand-mangled sidecar reads as ungraded rather than as an invented promotion.
_TIER_RANK = {"hunch": 0, "replay_screened": 1, _CONFIRMED_TIER: 2}

#: What the ``forward`` Stat is a statistic OF, stated on the wire so no caption has
#: to guess. The gold facet is deliberately narrow (a would_surface-stamped, actually
#: surfaced entry) and far thinner than the research grid; saying so beside the number
#: is what keeps a thin n from reading as a weak edge.
_FORWARD_SOURCE = (
    "forward shadow book, gold facet (would_surface) at arm=baseline / "
    "variant=default -- the exact slice pipeline.reflect grades; all-time, "
    "no trailing window"
)


def _best_tier(verdicts: list[Verdict]) -> str | None:
    """The highest rung any cell in this sidecar reached, or None if none did.

    None means UNGRADED -- an empty/missing/unreadable sidecar, or one carrying only
    tier strings outside the ladder. It is never defaulted to ``hunch``: "we have not
    measured this" and "we measured it and it is a hunch" are different claims, and
    only one of them is true of a book that was never reflected."""
    known = [v for v in verdicts if v.tier in _TIER_RANK]
    if not known:
        return None
    return max(known, key=lambda v: _TIER_RANK[v.tier]).tier


def _best_cohort(verdicts: list[Verdict], tier: str | None) -> dict[str, object] | None:
    """The strongest cell WITHIN ``tier``: the highest ``ci_low`` among its rows.

    Scoped to the best tier on purpose -- a hunch with a fatter bound is not better
    evidence, it is unproven evidence, and letting it win the cohort slot would put
    the board's headline number on the rung nothing has cleared. ``expectancy`` and
    ``ci_low`` route through ``_finite_or_none``: an empty bucket's bound is ``-inf``,
    which JSON cannot carry and which must never render as a measured number."""
    rows = [v for v in verdicts if v.tier == tier]
    if not rows:
        return None
    best = max(rows, key=lambda v: v.ci_low)
    return {
        "dimension": best.dimension,
        # The bucket rides along because ``dimension`` alone names no condition --
        # the gate's own confirmed-edge label is "<dimension>=<bucket>".
        "bucket": best.bucket,
        "expectancy": _finite_or_none(best.expectancy_r),
        "ci_low": _finite_or_none(best.ci_low),
        "n": best.n,
    }


def _calibration(gate_entry: dict[str, Any], *, play_type: str) -> dict[str, object]:
    """The gate's calibration verdict on the wire -- every number read STRAIGHT off
    ``CalibrationVerdict`` or off the floor constants, with no derivation in between.

    Takes the gate's whole per-play-type entry (not the verdict alone) because the
    countdown line reads ``ready`` from it too: one argument means the fractions and
    the "ready" they may collapse into can never describe different play types.

    ``countdown`` is ``autonomy._countdown_line`` VERBATIM ("reversal: 3/20 high,
    2/20 low, 2/8 tickers", or "<pt>: ready"), the same line the CLI report and the
    masthead render, whose format tests/pipeline/test_autonomy_countdown.py pins.
    That is also where the binding TICKER count lives: it is ``min(n_clusters_high,
    n_clusters_low)`` -- the gate's own rule that the shallower bucket gates -- and
    re-deriving it here would be a second definition of the countdown. Both raw
    cluster counts ride the wire, so a renderer never has to parse the sentence.

    ``ci_low`` / ``high_minus_low`` go through ``_finite_or_none``: an uncertifiable
    bound is ``-inf`` (``clustered_two_sample_delta_low``'s empty-book answer), and a
    served 'inf' would read as something we measured."""
    calib: CalibrationVerdict = gate_entry["calibration"]
    return {
        "countdown": _countdown_line(
            play_type, gate_entry,
            min_per_bucket=MIN_LEADERBOARD_N, cluster_floor=_CLUSTER_FLOOR),
        "calibrated": calib.calibrated,
        "n_high": calib.n_high,
        "n_low": calib.n_low,
        "n_clusters_high": calib.n_clusters_high,
        "n_clusters_low": calib.n_clusters_low,
        "min_per_bucket": MIN_LEADERBOARD_N,
        "cluster_floor": _CLUSTER_FLOOR,
        "ci_low": _finite_or_none(calib.ci_low),
        "high_minus_low": _finite_or_none(calib.high_minus_low),
        "reason": calib.reason,
    }


def _forward(session: Session, play_type: str) -> dict[str, object]:
    """This play type's forward-book evidence: a full Stat over the GOLD slice.

    ``load_closed_paper_trades`` pinned to (arm=BASELINE, variant=DEFAULT_VARIANT)
    then ``facet_filter(..., "gold")`` -- byte-for-byte the composition
    ``reflect.run_reflection`` grades (its ``forward_gold``), so the board's forward
    number and the verdicts beside it describe the SAME book. ``summarize`` does the
    math; nothing is computed here. An empty book is an honest zero-n Stat (never an
    absent key), and ``cost_level`` is ``cost_level_for``'s cutoff rule over the same
    rows the Stat was computed from."""
    gold = facet_filter(
        repo.load_closed_paper_trades(
            session, play_type=play_type, arm=BASELINE, variant=DEFAULT_VARIANT),
        "gold",
    )
    stat = stat_from_summary(
        summarize(gold), cost_level=cost_level_for(gold), corpus_id=None, facet="gold")
    return {"stat": stat.as_dict(), "source": _FORWARD_SOURCE}


def build_strategies_router(
    *,
    _session: Callable[[], Iterator[Session]],
    edge_dir: Path | None,
) -> APIRouter:
    """The Strategy Board's read endpoint, closed over the app's seams: the session
    dependency and the raw edge dir (resolved per request, the heartbeats seam)."""
    router = APIRouter()

    @router.get("/api/strategies")
    def strategies(session: Session = Depends(_session)) -> dict[str, object]:
        """Every play type, ranked by evidence, with the scope it is trading under.

        Per row:

        * ``in_ceiling`` -- membership in ``SWING_EXECUTE_PLAY_TYPES``, or ``null``
          when the env expresses NO ceiling. Null, not False: "unset" is not
          "excluded", and rendering an unset ceiling as OUT would invite an operator
          to "fix" it with an env deploy that changes nothing.
        * ``disabled`` -- the cockpit subtraction (``agent_guardrails
          .disabled_play_types``), the one knob the board itself can move.
        * ``effective`` -- membership in ``effective_scope_from_state``'s answer (a
          ``None`` result = unscoped = everything is in). The SAME function execution
          enforces, so the lamp can never say TRADING about a play type dispatch
          would drop.
        * ``playbook_present`` -- ``edge/<pt>.md`` AND the verdicts sidecar both
          exist. Both halves: prose whose numbers nothing backs is not a playbook.
        * ``tier`` / ``best_cohort`` -- the ladder and the strongest cell within the
          winning rung (see ``_best_tier`` / ``_best_cohort``). A missing or
          unreadable sidecar is ``null``/``null``, which is the same posture the gate
          takes; the LOUD ``unreadable`` marker lives on /api/playbooks, where a human
          is already looking, rather than on a 60s poll.
        * ``calibration`` / ``gate_ready`` / ``edge_confirmed`` -- the advisory
          autonomy gate's own per-play-type report (see ``_calibration``).
        * ``forward`` -- the gold forward book as a Stat + a ``source`` note (see
          ``_forward``).
        * ``rank`` -- 1-based position under the ranking law: tier DESC, then the
          cohort's ``ci_low`` DESC, ungraded LAST, ties broken by play type name so
          the order is stable across polls.

        Top level: ``effective_scope`` / ``ceiling`` (sorted lists, or ``null`` for
        "no scoping applies" -- the honest answer to two unset knobs, never a
        materialised vocabulary), ``disabled`` (sorted; the stored set VERBATIM, so a
        member outside today's vocabulary is still visible), ``as_of``, and
        ``ci_note`` -- the playbooks router's ONE statement of what a verdict's
        ``ci_low`` means, imported rather than re-worded.

        DB + FILES ONLY: no broker, no quotes. ``peek_guardrails`` (never the seeding
        ``load``) keeps the poll write-free -- see the module docstring's G7 note.
        """
        edir = resolve_edge_dir(edge_dir)
        settings = load_settings()
        # ONE brake snapshot for every scope field: a scope change landing between two
        # reads could otherwise publish a row that contradicts the header above it.
        g = guardrails_repo.peek_guardrails(session)
        scope = guardrails_repo.effective_scope_from_state(settings, g)
        ceiling = settings.execute_play_types
        # ONE gate evaluation for every row (it iterates PLAY_TYPES itself, so every
        # play type below is a key by construction).
        report = autonomy_gate(session, edge_dir=edir)

        ranked: list[tuple[tuple[int, float, str], dict[str, object]]] = []
        for pt in PLAY_TYPES:
            verdicts = _read_verdicts(edir, pt)
            tier = _best_tier(verdicts)
            cohort = _best_cohort(verdicts, tier)
            gate_entry = report.per_play_type[pt]
            row: dict[str, object] = {
                "play_type": pt,
                "rank": 0,  # assigned after the sort below
                "in_ceiling": None if ceiling is None else pt in ceiling,
                "disabled": pt in g.disabled_play_types,
                "effective": scope is None or pt in scope,
                "playbook_present": (
                    (edir / f"{pt}.md").exists()
                    and (edir / verdicts_filename(pt)).exists()
                ),
                "tier": tier,
                "best_cohort": cohort,
                "calibration": _calibration(gate_entry, play_type=pt),
                "gate_ready": bool(gate_entry["ready"]),
                "edge_confirmed": bool(gate_entry["edge_confirmed"]),
                "forward": _forward(session, pt),
            }
            # The sort key IS the ranking law: tier DESC, ci_low DESC, name ASC.
            # Ungraded ranks below every tier (-1) and an absent cohort sorts last
            # (-(-inf) = +inf) -- nulls last, never silently ahead of measured rows.
            tier_rank = _TIER_RANK.get(tier or "", -1)
            ci_low = cohort["ci_low"] if cohort else None
            floor = float(ci_low) if isinstance(ci_low, float) else float("-inf")
            ranked.append(((-tier_rank, -floor, pt), row))

        ranked.sort(key=lambda pair: pair[0])
        rows: list[dict[str, object]] = []
        for rank, (_key, row) in enumerate(ranked, start=1):
            row["rank"] = rank
            rows.append(row)

        return {
            "strategies": rows,
            "effective_scope": None if scope is None else sorted(scope),
            "ceiling": None if ceiling is None else sorted(ceiling),
            "disabled": sorted(g.disabled_play_types),
            "as_of": _utc_iso(datetime.now(UTC)),
            "ci_note": _CI_NOTE,
        }

    return router
