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

import math
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from swing_screener.cockpit.playbooks import playbook_drift
from swing_screener.db.models import AnalysisRequest
from swing_screener.pipeline.proposed import APPROVED, QUEUED, load_proposed_for
from swing_screener.pipeline.reflect import (
    _PLAY_TYPES,
    Verdict,
    _edge_text,
    _section_body,
    due_play_types,
    load_verdicts,
    parse_state,
)
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
          ok -- whenever the sidecar could not be read (missing or corrupt):
          UNKNOWN never renders green.
        * ``falsified``: the "Falsified / retired" section body via the
          reflection's own parser, so the UI can strike it through without
          re-parsing markdown.
        * ``reflection_due``: whether this play type's forward book has re-armed
          a reflection (``due_play_types`` -- the reflection's OWN gate, so the
          lamp and the nightly job agree by construction).
        """
        edir = resolve_edge_dir(edge_dir)
        due = due_play_types(session, edge_dir=edir)
        books: list[dict[str, object]] = []
        store_errors: list[str] = []
        for pt in _PLAY_TYPES:
            md = _edge_text(edir, pt)
            state = parse_state(md)
            rows: list[dict[str, object]] = []
            drift: dict[str, object] | None = None
            error: str | None = None
            sidecar = edir / f"{pt}.verdicts.json"
            if not sidecar.exists():
                error = "missing"
            else:
                try:
                    verdicts = load_verdicts(sidecar.read_text(encoding="utf-8"))
                    # Built inside the try, list-then-assign (the proposals-GET
                    # rule): a wrong-typed row must degrade the book, not leak a
                    # partially-built row list onto the wire.
                    rows = [_verdict_row(v) for v in verdicts]
                except (ValueError, TypeError) as exc:  # JSONDecodeError IS a ValueError
                    error = f"unreadable ({type(exc).__name__})"
                    store_errors.append(pt)
                else:
                    d = playbook_drift(md, verdicts)
                    drift = {
                        "ok": d.ok,
                        "missing": [{"token": m.token, "tier": m.tier}
                                    for m in d.missing],
                    }
            books.append({
                "play_type": pt,
                "md": md,
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
          visible state (an approval marks, a human promotes; the strip must keep
          saying so until the promotion commit lands and the row leaves the
          store). A corrupt store degrades QUIETLY here (that play type's names
          are simply absent) -- the loud ``store_errors`` marker lives on
          ``/api/proposals``, where a human is looking; a strip that 500s every
          60s helps nobody.
        * ``reflection_due``: the play types whose forward book re-armed a
          reflection (``due_play_types`` -- the same gate the nightly job runs).
        * ``latest_analysis_id``: ``max(AnalysisRequest.id)`` or null -- the
          client compares it to its localStorage last-seen id for the unread
          badge (Phase 3 plan, scope decision 14: unread is client-side).
        """
        edir = resolve_edge_dir(edge_dir)
        queued: list[str] = []
        approved: list[str] = []
        for pt in _PLAY_TYPES:
            try:
                items = load_proposed_for(pt, edir)
            except (ValueError, TypeError):
                continue  # degrade quietly; /api/proposals carries the loud marker
            queued += [pv.name for pv in items if pv.status == QUEUED]
            approved += [pv.name for pv in items if pv.status == APPROVED]
        return {
            "proposals_queued": queued,
            "proposals_approved_pending": approved,
            "reflection_due": due_play_types(session, edge_dir=edir),
            "latest_analysis_id": session.scalar(
                select(func.max(AnalysisRequest.id))),
        }

    return router


def _verdict_row(v: Verdict) -> dict[str, object]:
    """One sidecar verdict's wire form -- hand-rolled (the cockpit rule), every
    ``Verdict`` field by name so a field added to the dataclass is a conscious
    wire decision, not an accidental ``asdict`` leak. ``cost_level`` /
    ``corpus_id`` pass through as-is: None means the row predates provenance
    stamping and the UI renders "not measured" -- fabricating a default here is
    exactly the lie the North Star forbids. The two floats ride through
    ``_finite_or_none``: the grader never writes a non-finite value (an empty
    cell records the 0.0 placeholder), but ``json.loads`` ACCEPTS ``-Infinity``
    from a mangled sidecar and JSON cannot carry it back out -- the wire
    contract is null there (the profit-factor-inf precedent), stated HERE
    rather than left to the serializer's inf-handling default."""
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
        "cost_level": v.cost_level,
        "corpus_id": v.corpus_id,
    }


def _finite_or_none(value: float) -> float | None:
    """JSON has no inf/nan: a non-finite float from a hand-mangled sidecar
    serves as an explicit null (see ``_verdict_row``)."""
    return value if math.isfinite(value) else None
