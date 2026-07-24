"""The analyst-proposal surface: the decision-ready list plus the approve/withdraw
actions. Moved verbatim out of ``cockpit/api.py``; the store's error mapping
(corrupt -> 503 fixed detail, unknown -> 404, refused transition -> 409) and the
never-auto-promote posture are unchanged."""

import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from swing_screener.cockpit.common import ActionNonce, _require_cockpit
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.proposed import (
    APPROVED,
    PLAY_TYPES,
    ProposedVariant,
    decide_proposal,
    load_proposed_for,
    store_filename,
    to_config,
)
from swing_screener.settings import resolve_edge_dir


class ProposalDecision(BaseModel):
    """POST /api/proposals/{play_type}/{name}/approve|withdraw body: the decision
    reason -- required (non-blank after strip), <=200 chars. It lands VERBATIM in
    the store's rationale audit append (`` APPROVED <date>: <reason>``, the 2026-07
    hand-edit convention), so the bound is a sanity cap on an append-forever field,
    not a column width. No float fields, so there is no ``allow_inf_nan`` to pin
    (the template rule)."""

    reason: str = Field(max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason is required")
        return v


def build_proposals_router(
    *, edge_dir: Path | None, action_nonce: ActionNonce
) -> APIRouter:
    """The proposal endpoints, closed over the app's seams: the edge dir where
    the proposed stores live, and the post-action wake nonce (bumped by the two
    decision actions here). Filesystem only -- no session dependency."""
    router = APIRouter()

    @router.get("/api/proposals")
    def proposals() -> dict[str, object]:
        """The analyst's proposed screen variants, both play types, decision-ready.

        Continuation rows first, then reversal, FILE order within each (the draft
        merge appends, so file order is drafting order). Each row is the stored
        ``ProposedVariant``'s 8 fields verbatim plus three derived decision aids --
        ``gate_verdict``, ``delta_vs_incumbent``, ``noop`` (semantics + leak posture
        in ``_proposal_row``) -- computed against the incumbent ``StrategyConfig()``,
        the same base the optimizer sweeps -- plus ``promotion_checklist``: the
        verbatim three-artifact checklist on every APPROVED row (null otherwise),
        so the next-steps card survives a refresh instead of living only in the
        transient approve response. A missing proposed.json is a play type
        with nothing queued: its rows are simply absent, never an error. A CORRUPT
        store (malformed JSON, a row ``load_proposed`` can't rebuild, OR a row with
        the right keys but wrong TYPES -- e.g. ``"delta": 1.5`` -- that only fails
        when ``_proposal_row`` renders it) degrades per play type: its rows are
        absent and ``store_errors`` names it, while the healthy play type still
        renders -- one broken file never blanks the screen. ``store_errors``
        carries play-type names ONLY, never the parser message or a path (leak
        posture). Filesystem only -- no DB session, so a down database never blanks
        this screen either."""
        edir = resolve_edge_dir(edge_dir)
        base = StrategyConfig()
        rows: list[dict[str, object]] = []
        store_errors: list[str] = []
        for pt in PLAY_TYPES:
            try:
                items = load_proposed_for(pt, edir)
                # Rows are BUILT inside the try, list-then-extend -- never
                # extend-with-generator, which would append the healthy rows a lazy
                # generator had already yielded before the wrong-TYPES row raised,
                # leaking a partial store onto the wire.
                built = [_proposal_row(pv, base, pt) for pv in items]
            except (ValueError, TypeError):  # JSONDecodeError IS a ValueError
                store_errors.append(pt)
                continue
            rows.extend(built)
        return {"proposals": rows, "store_errors": store_errors}

    def _decide(
        play_type: str, name: str, *,
        decision: Literal["approved", "withdrawn"], reason: str,
    ) -> ProposedVariant:
        """Shared decision plumbing: ``decide_proposal`` owns the state machine;
        this maps its errors onto the wire. A CORRUPT store is a 503 with a FIXED
        detail (trivially leak-clean; the parser message adds nothing a hand-fix
        needs) -- and that handler's ORDER is load-bearing: ``json.JSONDecodeError``
        IS a ValueError, so listed after the 409 arm a typo'd store would read as
        'already decided'; the TypeError flavor is a row ``load_proposed`` can't
        rebuild (``ProposedVariant(**d)``). Then KeyError (unknown name) -> 404
        with the exception's own message (``args[0]``, never ``str(exc)`` --
        str(KeyError) wraps the message in quotes); ValueError (refused
        transition) -> 409 naming the current status. Every text is our own
        fixed/store prose -- no paths, no client input. The server stamps
        ``today``; the client's clock never dates an audit append."""
        try:
            return decide_proposal(
                resolve_edge_dir(edge_dir), play_type, name,
                decision=decision, reason=reason,
                today=datetime.now(UTC).date().isoformat(),
            )
        except (json.JSONDecodeError, TypeError) as exc:  # BEFORE ValueError
            raise HTTPException(
                status_code=503,
                detail=("proposal store unreadable -- fix "
                        f"edge/{store_filename(play_type)} by hand"),
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/proposals/{play_type}/{name}/approve",
                 dependencies=[Depends(_require_cockpit)])
    def approve_proposal(
        play_type: Literal["continuation", "reversal"],
        name: str,
        body: ProposalDecision,
    ) -> dict[str, object]:
        """Approve one QUEUED proposal -- approve MARKS, never promotes.

        Header-guarded (``_require_cockpit``); the ``reason`` lands verbatim in the
        rationale audit append. The ONLY write is the store flip ``decide_proposal``
        makes -- an uncommitted working-tree edit (the response's ``note`` says so);
        nothing here touches variants.py or experiments.json. Promotion stays the
        three human edits in the response's ``checklist`` (roster line, registry
        row, this flip), landed as ONE commit -- North Star #1: evidence gates
        promotion, and a tool never promotes. 404/409 mapping in ``_decide``."""
        pv = _decide(play_type, name, decision="approved", reason=body.reason)
        action_nonce.bump()  # post-action wake: the store was atomically replaced
        out = _decision_dict(pv, play_type)
        out["checklist"] = _promotion_checklist(play_type)
        return out

    @router.post("/api/proposals/{play_type}/{name}/withdraw",
                 dependencies=[Depends(_require_cockpit)])
    def withdraw_proposal(
        play_type: Literal["continuation", "reversal"],
        name: str,
        body: ProposalDecision,
    ) -> dict[str, object]:
        """Withdraw one proposal -- legal from QUEUED and from APPROVED (an
        approval can be walked back; a withdrawal is final until a human
        hand-edits the store). Same guard, audit append, working-tree honesty and
        404/409 mapping as approve; no checklist -- there is nothing to promote."""
        pv = _decide(play_type, name, decision="withdrawn", reason=body.reason)
        action_nonce.bump()  # post-action wake: the store was atomically replaced
        return _decision_dict(pv, play_type)

    return router


def _proposal_row(
    pv: ProposedVariant, base: StrategyConfig, store_pt: str
) -> dict[str, object]:
    """One proposal's wire form -- hand-rolled like ``_beat_dict``, never ``asdict``.

    ``gate_verdict`` runs the REAL gatekeeper (``proposed.to_config``) inline: 'ok',
    or the ValueError text VERBATIM -- safe by construction, it is config-knob prose
    from our own code (no paths, no client input), the same verdict the optimizer
    logs when it skips the row. ``noop`` mirrors ``optimize.build_config_grid``'s
    no-op guard EXACTLY -- its ``cfg == base`` check on ``to_config``'s output (a
    validated delta equal to the incumbent can never beat it) -- so a row that FAILS
    the gate is invalid, not a no-op: ``noop`` stays False and the verdict says why.
    ``delta_vs_incumbent`` reads ``current`` off the incumbent config only for REAL
    dataclass fields; an unknown knob's current is null -- never a blind ``getattr``,
    which would hand a method repr to a delta key that happened to name one.
    ``promotion_checklist`` is the same verbatim three-artifact list the approve
    response carries, on every APPROVED row (null otherwise) -- parameterized by
    ``store_pt``, the play type of the FILE the row was loaded from (the
    ``_decision_dict`` rule: a hand-mangled row's stored ``play_type`` can never
    mislabel the store the checklist names)."""
    verdict = "ok"
    noop = False
    try:
        noop = to_config(pv, base) == base
    except ValueError as exc:
        verdict = str(exc)
    known = {f.name for f in fields(base)}
    return {
        "name": pv.name,
        "play_type": pv.play_type,
        "delta": dict(pv.delta),
        "rationale": pv.rationale,
        "hunch_ref": pv.hunch_ref,
        "status": pv.status,
        "drafted_at": pv.drafted_at,
        "provenance": pv.provenance,
        "gate_verdict": verdict,
        "delta_vs_incumbent": [
            {"knob": k, "current": getattr(base, k) if k in known else None,
             "proposed": v}
            for k, v in pv.delta.items()
        ],
        "noop": noop,
        "promotion_checklist": (
            _promotion_checklist(store_pt) if pv.status == APPROVED else None
        ),
    }


def _decision_dict(pv: ProposedVariant, play_type: str) -> dict[str, object]:
    """The shared approve/withdraw wire form: the row's new state plus WHERE the
    write landed. ``play_type``/``file`` come from the URL path param -- the value
    that actually keyed ``decide_proposal``'s store write -- NOT the row's stored
    field, so a hand-mangled row whose ``play_type`` disagrees with the file it
    lives in can never mislabel the file touched. ``file`` is the store's
    REPO-RELATIVE label, never the resolved edge dir (same leak posture as
    ``connection_label``: paths stay off the wire); the ``note`` is the honesty
    line -- ``decide_proposal`` edits the working tree only, and git capturing the
    flip is the human's move."""
    return {
        "name": pv.name,
        "play_type": play_type,
        "status": pv.status,
        "file": f"edge/{store_filename(play_type)}",
        "note": "uncommitted working-tree edit — commit with your decision",
    }


def _promotion_checklist(play_type: str) -> list[str]:
    """The three-artifact promotion checklist, verbatim (Phase 3 plan, Task 8): an
    approval marks ONE row; promotion is these three coupled edits, all human, one
    commit -- the registry charter's shape (roster line so the optimizer sweeps it,
    registry row so settlement can grade it, the store flip this endpoint already
    made)."""
    return [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        ("2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)"),
        f"3. edge/{store_filename(play_type)} — this flip (done)",
    ]
