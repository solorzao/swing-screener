"""The validated store for analyst-QUEUED screen variants (Phase-6 Task 5).

North Star #1 (evidence gates promotion): the LLM analyst (Task 6) drafts candidate SCREEN
VARIANTS on a "needs a test" hunch -- it QUEUES them here, it does NOT promote anything. A
queued variant is just a ``StrategyConfig`` delta + provenance; the optimizer then merges the
queued set into its walk-forward sweep, and promotion to the live ``config.py`` stays the
existing human-gated ``propose()`` PR gate.

This module is the validated DATA layer (no LLM, no network):

  * ``ProposedVariant`` -- a frozen, JSON-native record of one queued variant.
  * ``proposed_to_json`` / ``load_proposed`` -- a lossless JSON round-trip (mirroring
    ``reflect.verdicts_to_json`` / ``load_verdicts``).
  * ``to_config`` -- THE GATEKEEPER. It turns a delta into a ``StrategyConfig`` and VALIDATES
    it, so a malformed or illegal delta NEVER reaches the optimizer grid. It rejects (a) a
    delta touching a frozen INDICATOR field (those would need their own enriched frames, which
    the shared-frame shadow book does not build) by reusing ``variants._assert_shared_indicators``,
    and (b) an UNKNOWN ``StrategyConfig`` field (``dataclasses.replace`` raises ``TypeError``;
    we re-raise a clear ``ValueError``).
  * ``load_proposed_for`` -- reads one play type's ``edge/<pt>.proposed.json`` (missing -> []).
  * ``decide_proposal`` -- the human decision (approve / withdraw) as a code-owned transition
    (Phase 3): the cockpit's replacement for the 2026-07 hand-edited ``WITHDRAWN <date>: ...``
    convention, with the same rationale-append audit trail.
"""

import dataclasses
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from swing_screener.config import StrategyConfig
from swing_screener.pipeline import variants

# A queued variant is ready for the sweep; other statuses (e.g. "draft", "promoted",
# "rejected") are NOT swept. The optimizer merges only ``QUEUED`` variants.
QUEUED = "queued"
# The two DECIDED statuses ``decide_proposal`` writes (Phase 3). Both retire the row from
# the sweep (only ``QUEUED`` is swept); an approved row's promotion path is the human-gated
# ``propose()`` PR, never another walk-forward slot.
APPROVED = "approved"
WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ProposedVariant:
    """One analyst-drafted candidate screen variant awaiting a walk-forward test.

    ``delta`` is the ``StrategyConfig`` override (a flat dict of JSON-native scalars); it may
    only touch DETECTION / zone / scoring knobs -- never a frozen indicator period (enforced by
    ``to_config``). ``hunch_ref`` ties the variant back to the reflection verdict (the
    "needs a test" hunch) that motivated it; ``provenance`` records who/what drafted it.
    ``status`` is ``"queued"`` for a variant the optimizer should sweep.
    """

    name: str
    play_type: str
    delta: dict[str, float | int | str | bool]
    rationale: str
    hunch_ref: str
    status: str
    drafted_at: str
    provenance: str


def to_config(pv: ProposedVariant, base: StrategyConfig) -> StrategyConfig:
    """Turn a queued variant's delta into a validated ``StrategyConfig`` (the GATEKEEPER).

    ``dataclasses.replace(base, **delta)`` applies the override, then
    ``variants._assert_shared_indicators`` refuses a delta that retunes a frozen indicator
    period (it would score against the wrong reused frame). An UNKNOWN key makes ``replace``
    raise ``TypeError``, which we catch and re-raise as a clear ``ValueError`` naming the
    variant and the offending key. A malformed/illegal delta therefore raises HERE and never
    reaches the optimizer grid.
    """
    try:
        # The delta is a heterogeneous scalar dict by design (it is user/analyst data, not a
        # statically-known kwargs shape), so mypy can't reconcile the **unpack with the typed
        # fields. That's exactly why this is the runtime gatekeeper: ``replace`` raises on an
        # unknown field (caught below) and ``_assert_shared_indicators`` rejects an illegal one.
        cfg = dataclasses.replace(base, **pv.delta)  # type: ignore[arg-type]
    except TypeError as exc:
        known = {f.name for f in dataclasses.fields(base)}
        unknown = sorted(set(pv.delta) - known)
        raise ValueError(
            f"proposed variant {pv.name!r} has unknown StrategyConfig field(s) "
            f"{unknown}: {exc}"
        ) from exc
    variants._assert_shared_indicators(base, pv.name, cfg)
    return cfg


# ===========================================================================
# SERIALIZE -- the code-owned proposed-variants sidecar (mirrors verdicts.json).
#
# ``ProposedVariant`` is a flat frozen dataclass of JSON-native scalars (its one nested field,
# ``delta``, is a dict of scalars), so ``asdict`` / ``ProposedVariant(**d)`` round-trips every
# field losslessly -- exactly like ``reflect.verdicts_to_json`` / ``load_verdicts``.
# ===========================================================================


def proposed_to_json(items: list[ProposedVariant]) -> str:
    """Serialize queued variants to the machine-readable sidecar JSON (lossless)."""
    return json.dumps([asdict(pv) for pv in items], indent=2)


def load_proposed(text: str) -> list[ProposedVariant]:
    """Inverse of ``proposed_to_json``: parse the sidecar JSON back into ``ProposedVariant``s."""
    return [ProposedVariant(**d) for d in json.loads(text)]


def store_filename(play_type: str) -> str:
    """The ONE owner of the proposal store's filename shape: one play type's store
    lives at ``<edge_dir>/<this>``. Every reader/writer (here; ``reflect``'s draft
    merge) and every wire label naming the file (the cockpit's decision responses)
    derives from this function, so the pattern cannot drift apart across sites."""
    return f"{play_type}.proposed.json"


def load_proposed_for(play_type: str, edge_dir: Path) -> list[ProposedVariant]:
    """Read one play type's queued variants from ``edge/<play_type>.proposed.json``.

    A missing file returns ``[]`` (a play type with nothing queued is the common case).
    """
    path = edge_dir / store_filename(play_type)
    if not path.exists():
        return []
    return load_proposed(path.read_text(encoding="utf-8"))


def _write_proposed(path: Path, items: list[ProposedVariant]) -> None:
    """Atomically rewrite one proposal store: serialize to a ``.tmp`` sibling, then
    ``os.replace`` it over the real file.

    This file holds the ONLY uncommitted copy of human decisions (until git captures them),
    and a torn write is the one corruption the draft merge's fail-safe read guard cannot
    undo -- so the store is never left half-written. Used by BOTH proposal-store writers
    (``decide_proposal`` here, ``reflect.draft_variants``' merge).
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(proposed_to_json(items), encoding="utf-8")
    os.replace(tmp, path)


def decide_proposal(
    edge_dir: Path,
    play_type: str,
    name: str,
    *,
    decision: Literal["approved", "withdrawn"],
    reason: str,
    today: str,
) -> ProposedVariant:
    """Record a human decision on ONE proposal -- the first TOOL-written status transition.

    The 2026-07 convention was a hand edit: flip ``status`` and append ``WITHDRAWN <date>:
    <reason>`` to the rationale. Phase 3's cockpit writes the same record through code, and
    the data layer owns the state machine:

      * approve  -- requires ``status == QUEUED`` (only a still-queued idea can be approved);
      * withdraw -- requires ``status`` in ``(QUEUED, APPROVED)`` (an approval can be walked
        back; a withdrawal is final until a human hand-edits it).

    ``decision`` is also RUNTIME-checked (``Literal`` is static-only): anything but
    ``APPROVED``/``WITHDRAWN`` raises ``ValueError`` rather than inventing a status the state
    machine doesn't know -- the same gatekeeper stance as ``to_config``. An unknown ``name``
    raises ``KeyError``; a refused transition raises ``ValueError`` naming the current status,
    and the store is left untouched. On success the ONE row is replaced
    (``dataclasses.replace``: new ``status``, rationale + `` {DECISION} {today}: {reason}`` --
    byte-compatible with the hand-edit convention) and the WHOLE file is rewritten atomically
    (``_write_proposed``) with every other row and the row order preserved. Returns the updated
    row. The decided row then stays in the file as the audit record (``draft_variants``' merge
    preserves non-queued rows) until git captures it.
    """
    if decision not in (APPROVED, WITHDRAWN):
        raise ValueError(
            f"unknown decision {decision!r}: must be {APPROVED!r} or {WITHDRAWN!r}"
        )
    items = load_proposed_for(play_type, edge_dir)
    index = next((i for i, pv in enumerate(items) if pv.name == name), None)
    if index is None:
        raise KeyError(f"no proposal named {name!r} for {play_type}")
    current = items[index]
    allowed = (QUEUED,) if decision == APPROVED else (QUEUED, APPROVED)
    if current.status not in allowed:
        raise ValueError(
            f"cannot mark proposal {name!r} {decision}: its status is {current.status!r} "
            f"(allowed from: {', '.join(allowed)})"
        )
    updated = dataclasses.replace(
        current,
        status=decision,
        rationale=current.rationale + f" {decision.upper()} {today}: {reason}",
    )
    items[index] = updated
    _write_proposed(edge_dir / store_filename(play_type), items)
    return updated
