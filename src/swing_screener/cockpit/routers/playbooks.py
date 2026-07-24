"""The Playbooks screen's read (``/api/playbooks``) and the Needs-Your-Hand strip's
permanent-poll feed (``/api/attention``) -- one router because both read the same
edge-dir stores (md playbooks, verdicts sidecars, proposal stores) plus the same
cheap DB facts, and neither touches a broker, a quote, or a resolver.

HONESTY POSTURE (North Star): every NUMBER on the Playbooks screen comes from the
code-owned ``edge/<pt>.verdicts.json`` SIDECAR -- the md is served VERBATIM as prose
and never parsed for figures. Parsing reuses the reflection's OWN loaders
(``load_verdicts``, ``parse_state``, ``_section_body``, ``due_play_types``) so this
surface and ``pipeline/reflect.py`` agree by construction and can never fork a
second parser. Promotion is a HUMAN act -- nothing here mutates anything; both
endpoints are plain GETs.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.cockpit.common import _finite_or_none
from swing_screener.cockpit.gh import open_research_prs
from swing_screener.cockpit.playbooks import DriftReport, playbook_drift
from swing_screener.db.models import (
    AnalysisRequest,
    JournalReview,
    JournalTradeTag,
    SystemAudit,
)
from swing_screener.pipeline.proposed import (
    APPROVED,
    PLAY_TYPES,
    QUEUED,
    load_proposed_for,
)
from swing_screener.pipeline.reflect import (
    Verdict,
    _edge_text,
    _section_body,
    due_play_types,
    load_verdicts,
    parse_state,
    verdicts_filename,
)
from swing_screener.pipeline.registry import load_experiments
from swing_screener.settings import resolve_edge_dir

# The one wire statement of what a verdict's bound IS -- served with every playbooks
# response so no renderer has to guess: ``Verdict`` carries NO upper bound, and its
# ``ci_low`` is not a plain 95% bound but the multiple-comparisons-corrected one
# (``reflect._ALPHA`` over the frozen family). Distinct from the Stat dict's
# ``ci_low``/``ci_high`` pair on purpose -- tier rides verdict rows, never a Stat
# (Phase 3 plan, scope decision 11).
_CI_NOTE = (
    "ci_low is the Bonferroni-corrected one-sided lower bound on the deciding "
    "book; verdicts carry no ci_high"
)

# The Auditor's closed severity vocabulary (``journal/audit_run.py`` writes exactly
# these; the column defaults to "info"), ranked so ``audit_worst`` can pick the
# gravest unacked row. An out-of-vocab string (a hand-mangled row) ranks lowest --
# a real warn/alert always outranks garbage -- and is served verbatim, never
# laundered into a severity nobody wrote.
_SEVERITY_RANK = {"info": 0, "warn": 1, "alert": 2}


def build_playbooks_router(
    *,
    _session: Callable[[], Iterator[Session]],
    edge_dir: Path | None,
) -> APIRouter:
    """The playbooks + attention endpoints, closed over the app's seams: the
    session dependency (reflection-due and the latest-analysis watermark are DB
    facts) and the raw edge dir (resolved per request, the heartbeats seam)."""
    router = APIRouter()

    @router.get("/api/playbooks")
    def playbooks(session: Session = Depends(_session)) -> dict[str, object]:
        """Both play types' playbooks, sidecar-first (module docstring).

        Per book:

        * ``md``: the edge file VERBATIM ("" when missing -- ``_edge_text``'s
          posture); prose only, numbers on screen come from ``verdicts``.
        * ``md_error``: None when the md was read (missing included -- that is a
          setup state); ``"unreadable (ClassName)"`` when the read RAISED -- a
          cp1252 smart-quote from a Windows hand-edit is this surface's own
          threat model. ``reflect._edge_text`` stays STRICT by decision (the
          nightly reflection must fail loudly on a corrupt file); the ENDPOINT
          degrades instead: md serves "", drift is unknown, and the sidecar --
          whose numbers never depended on the prose -- still renders.
        * ``frontmatter``: the reflection state ``parse_state`` reads (counter +
          ``last_reflected``, tolerant of hand-edits).
        * ``verdicts``: the sidecar rows, EVERY field including the provenance
          stamps -- ``cost_level``/``corpus_id`` are None on pre-Phase-3 rows and
          are served as None, never backfilled (an honest "not measured"). An
          ``n=0`` row is "empty on this corpus", not an error. ``ci_note`` (top
          level) states the bound's meaning once.
        * ``verdicts_error``: None when the sidecar was read; ``"missing"`` for a
          never-reflected play type (a setup state, like a missing proposal
          store); ``"unreadable (ClassName)"`` for a corrupt one -- class name
          only, never the parser message (leak posture), and the play type ALSO
          lands in top-level ``store_errors`` (the proposals-GET precedent). One
          broken sidecar never blanks the other book.
        * ``drift``: the structural md-vs-sidecar check (``cockpit.playbooks``),
          an ADVISORY amber. ``None`` -- explicitly unknown, never a fabricated
          ok -- whenever EITHER input could not be read (md unreadable, sidecar
          missing or corrupt): faithfulness against nothing is unknowable, and
          UNKNOWN never renders green.
        * ``falsified``: the "Falsified / retired" section body via the
          reflection's own parser, so the UI can strike it through without
          re-parsing markdown.
        * ``reflection_due``: whether this play type's forward book has re-armed
          a reflection (``due_play_types`` -- the reflection's OWN gate, so the
          lamp and the nightly job agree by construction). The gate itself reads
          every md, so an unreadable one degrades it to nothing-due for BOTH
          books -- quiet by design; ``md_error`` is the loud marker here.
        """
        edir = resolve_edge_dir(edge_dir)
        due = _due_or_empty(session, edir)
        books: list[dict[str, object]] = []
        store_errors: list[str] = []
        for pt in PLAY_TYPES:
            md_error: str | None = None
            try:
                md = _edge_text(edir, pt)
            except (OSError, ValueError) as exc:  # UnicodeDecodeError IS a ValueError
                md = ""
                md_error = f"unreadable ({type(exc).__name__})"
            state = parse_state(md)
            rows: list[dict[str, object]] = []
            drift: dict[str, object] | None = None
            error: str | None = None
            sidecar = edir / verdicts_filename(pt)
            if not sidecar.exists():
                error = "missing"
            else:
                try:
                    verdicts = load_verdicts(sidecar.read_text(encoding="utf-8"))
                    # Built inside ONE umbrella, list-then-assign (the
                    # proposals-GET rule) -- and the drift check lives inside it
                    # too: a row that PASSES ``Verdict(**d)`` with a mangled
                    # value (an unhashable list tier, say) first raises in
                    # ``playbook_drift``, and that is the same corrupt-store
                    # state, not a 500.
                    built = [_verdict_row(v) for v in verdicts]
                    checked = (None if md_error is not None
                               else _drift_dict(playbook_drift(md, verdicts)))
                except (ValueError, TypeError) as exc:  # JSONDecodeError IS a ValueError
                    error = f"unreadable ({type(exc).__name__})"
                    store_errors.append(pt)
                else:
                    rows = built
                    drift = checked
            books.append({
                "play_type": pt,
                "md": md,
                "md_error": md_error,
                "frontmatter": {
                    "forward_closed_at_last_reflection":
                        state.forward_closed_at_last_reflection,
                    "last_reflected": state.last_reflected,
                },
                "verdicts": rows,
                "verdicts_error": error,
                "drift": drift,
                "falsified": _section_body(md, "Falsified / retired"),
                "reflection_due": pt in due,
            })
        return {"books": books, "due_play_types": due,
                "store_errors": store_errors, "ci_note": _CI_NOTE}

    @router.get("/api/attention")
    def attention(session: Session = Depends(_session)) -> dict[str, object]:
        """The Needs-Your-Hand strip's permanent-poll feed: cheap file + DB reads
        only -- no broker, no quotes, no resolver (this rides the App's permanent
        roster, so its budget is the contract).

        * ``proposals_queued`` / ``proposals_approved_pending``: proposal NAMES by
          status, both play types -- approved-pending-promotion is a DISTINCT
          visible state (an approval marks, a human promotes). The promotion
          commit flips nothing back in the store, so "promoted" is detected by
          CROSS-CHECK: an approved name that exists as an ``edge/experiments.json``
          registry row (ANY status -- a 'retired' row still proves the experiment
          existed, i.e. was promoted) leaves this list; approved names absent
          from the registry keep nagging. An unreadable registry excludes
          nothing -- a nag that persists beats one that silently vanishes. A
          corrupt store degrades QUIETLY here (that play type's names are simply
          absent) -- the loud ``store_errors`` marker lives on
          ``/api/proposals``, where a human is looking; a strip that 500s every
          60s helps nobody.
        * ``reflection_due``: the play types whose forward book re-armed a
          reflection (``due_play_types`` -- the same gate the nightly job runs).
          The gate reads every edge md; an unreadable one (the cp1252 hand-edit
          case) degrades this to ``[]`` quietly -- same posture as the corrupt
          proposal store -- with the loud ``md_error`` marker on
          ``/api/playbooks`` where a human is looking.
        * ``latest_analysis_id``: ``max(AnalysisRequest.id)`` or null -- the
          client compares it to its localStorage last-seen id for the unread
          badge (Phase 3 plan, scope decision 14: unread is client-side).
        * ``audit_unacked`` / ``audit_worst``: how many SystemAudit rows (weekly
          reports AND breaches -- both are ack-able, the audit router's own
          semantics) still await the human ack, and the gravest severity among
          them (the Auditor's info < warn < alert; null when none). One grouped
          COUNT -- no row scan.
        * ``coach_pending``: per-trade Coach reviews carrying PARKED auto-tag
          proposals whose trade has no ``source="analyst"`` tag yet -- i.e. the
          confirm gate has not been exercised (confirming writes the overlay
          tag but leaves ``facts_json`` untouched, so the tag row is the only
          durable evidence). One aggregate COUNT over a serialized-shape LIKE
          (``json.dumps`` in ``routers/trades.py`` writes the
          ``"tag_proposals": [{`` shape being matched) -- no row scan.
        * ``research_prs``: OPEN reflection/optimizer PRs (``gh.open_research_prs``
          -- TTL-cached like the workflow poller; unconfigured token or any
          polling failure is an honest ``[]``, never an error).
        """
        edir = resolve_edge_dir(edge_dir)
        promoted = _promoted_names(edir)
        queued: list[str] = []
        approved: list[str] = []
        for pt in PLAY_TYPES:
            try:
                items = load_proposed_for(pt, edir)
            except (ValueError, TypeError):
                continue  # degrade quietly; /api/proposals carries the loud marker
            queued += [pv.name for pv in items if pv.status == QUEUED]
            approved += [pv.name for pv in items
                         if pv.status == APPROVED and pv.name not in promoted]
        # `== False` renders `= 0`; `.is_(False)` renders `IS 0`, which SQL Server
        # rejects -- and the desktop app polls this against Azure SQL.
        severities = session.execute(
            select(SystemAudit.severity, func.count(SystemAudit.id))
            .where(SystemAudit.acknowledged_by_human == False)
            .group_by(SystemAudit.severity)
        ).all()
        confirmed = select(JournalTradeTag.id).where(
            JournalTradeTag.trade_id == JournalReview.trade_id,
            JournalTradeTag.book == JournalReview.book,
            JournalTradeTag.source == "analyst",
        ).exists()
        coach_pending = session.scalar(
            select(func.count(JournalReview.id)).where(
                JournalReview.trade_id.is_not(None),
                JournalReview.facts_json.like(
                    '%"tag_proposals": \\[{%', escape="\\"),
                ~confirmed,
            )
        )
        return {
            "proposals_queued": queued,
            "proposals_approved_pending": approved,
            "reflection_due": _due_or_empty(session, edir),
            "latest_analysis_id": session.scalar(
                select(func.max(AnalysisRequest.id))),
            "audit_unacked": sum(n for _, n in severities),
            "audit_worst": max(
                (s for s, _ in severities),
                key=lambda s: _SEVERITY_RANK.get(s, -1), default=None),
            "coach_pending": coach_pending or 0,
            "research_prs": open_research_prs(),
        }

    return router


def _promoted_names(edir: Path) -> frozenset[str]:
    """Names with an ``edge/experiments.json`` registry row -- the registry's OWN
    loader (``pipeline.registry.load_experiments``; the forward-books endpoint's
    precedent), never a second parser. ALL statuses count: promotion is the human
    commit that CREATES the registry row, so a later 'retired' does not un-promote.
    Unreadable/corrupt registry -> empty set: with no readable evidence of
    promotion, every approved row keeps nagging (the safe direction -- the loud
    corrupt-file marker belongs to the surfaces a human reads)."""
    try:
        return frozenset(e.name for e in load_experiments(edir))
    except (OSError, ValueError, TypeError):  # JSONDecodeError IS a ValueError
        return frozenset()


def _due_or_empty(session: Session, edir: Path) -> list[str]:
    """``due_play_types`` with the endpoints' degrade posture: the gate reads
    every edge md through the STRICT ``reflect._edge_text`` (strict by decision
    -- the nightly reflection must fail loudly on a corrupt file), so an
    unreadable md here answers nothing-due instead of a 500. Quiet on purpose:
    the loud marker is the per-book ``md_error`` on ``/api/playbooks``. DB
    errors are NOT swallowed -- they propagate to the app's 503 posture."""
    try:
        return due_play_types(session, edge_dir=edir)
    except (OSError, ValueError):  # UnicodeDecodeError IS a ValueError
        return []


def _drift_dict(d: DriftReport) -> dict[str, object]:
    """The drift report's wire form (hand-rolled, like every cockpit serializer)."""
    return {
        "ok": d.ok,
        "missing": [{"token": m.token, "tier": m.tier} for m in d.missing],
    }


def _verdict_row(v: Verdict) -> dict[str, object]:
    """One sidecar verdict's wire form -- hand-rolled (the cockpit rule), every
    ``Verdict`` field by name so a field added to the dataclass is a conscious
    wire decision, not an accidental ``asdict`` leak. ``cost_level`` /
    ``corpus_id`` pass through as-is: None means the row predates provenance
    stamping and the UI renders "not measured" -- fabricating a default here is
    exactly the lie the North Star forbids. The two floats ride through the
    shared ``common._finite_or_none``: the grader never writes a non-finite
    value (an empty cell records the 0.0 placeholder), but ``json.loads``
    ACCEPTS ``-Infinity`` from a mangled sidecar and JSON cannot carry it back
    out -- the wire contract is null there."""
    return {
        "play_type": v.play_type,
        "dimension": v.dimension,
        "bucket": v.bucket,
        "tier": v.tier,
        "n": v.n,
        "expectancy_r": _finite_or_none(v.expectancy_r),
        "ci_low": _finite_or_none(v.ci_low),
        "n_clusters": v.n_clusters,
        "source": v.source,
        # Both books' depths on every row (the 2026-07-12 hunch-display fix): which
        # book n/expectancy_r/ci_low describe is auditable on the wire, not just in
        # the sidecar. Pre-fix rows lack the keys and load as the dataclass's 0 --
        # served as 0 (the reflection's own "no recorded depth"), the same
        # convention `load_verdicts` gives every other consumer.
        "n_forward": v.n_forward,
        "n_replay": v.n_replay,
        "cost_level": v.cost_level,
        "corpus_id": v.corpus_id,
    }
