"""Machine-readable experiment pre-registration (edge/experiments.json).

An Experiment row is the settlement card's source of truth: the stopping rule renders
VERBATIM with the sha it was registered under. Fields are never inferred at render time
-- a rule invented when the card is drawn is pre-registration theater, the exact failure
this file exists to prevent. Legacy experiments carry provenance describing where the
original wording lived (roster comment / study doc) and the sha that introduced the
roster line.
"""

import dataclasses
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class Experiment:
    name: str  # must equal the PaperTrade.arm or .variant string
    kind: str  # 'arm' | 'variant'
    play_type: str  # 'continuation' | 'reversal' | 'all' (delta scope)
    control: str  # 'baseline' (arms) | 'default' (variants)
    hypothesis: str
    stopping_rule: str  # rendered verbatim on the card
    mde_r: float  # futility: corrected upper bound < mde_r
    target_ci_halfwidth_r: float  # settlement: delta CI half-width <= this
    registered_at: str  # ISO date
    registered_sha: str  # git sha stamped at registration
    doc_ref: str
    provenance: str
    status: str  # 'active' | 'retired'
    decided_at: str | None = None
    decision: str | None = None

    def as_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def load_experiments(edge_dir: Path) -> list[Experiment]:
    path = edge_dir / "experiments.json"
    if not path.exists():
        return []
    return [Experiment(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def save_experiments(edge_dir: Path, experiments: list[Experiment]) -> None:
    """Atomically rewrite the registry (write-temp + replace, same durability
    posture as the proposal stores): a crash mid-write must never leave a
    half-file where the pre-registration audit record lives. Preserves the
    file's committed formatting (2-space indent, trailing newline) so the
    working-tree diff a decision produces is exactly the decided row."""
    path = edge_dir / "experiments.json"
    text = json.dumps([asdict(e) for e in experiments], indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(edge_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def decide_experiment(
    edge_dir: Path, name: str, *, decision: str, today: date | None = None
) -> Experiment:
    """Retire one ACTIVE experiment: status -> 'retired' with ``decided_at`` +
    the verbatim ``decision`` text. The registry edit is the AUDIT half of a
    settlement decision (docs/cockpit.md 'To retire'); the roster-line deletion
    stays a human source edit, and the lockstep CI test enforces the pairing at
    commit time. Raises ``KeyError`` for an unknown name and ``ValueError`` when
    the row is not active (already decided) -- the cockpit maps these to
    404/409."""
    experiments = load_experiments(edge_dir)
    by_name = {e.name: e for e in experiments}
    if name not in by_name:
        raise KeyError(f"no registered experiment named {name!r}")
    row = by_name[name]
    if row.status != "active":
        raise ValueError(f"{name} is already {row.status} -- nothing to decide")
    decided = dataclasses.replace(
        row,
        status="retired",
        decided_at=(today or datetime.now(UTC).date()).isoformat(),
        decision=decision,
    )
    save_experiments(
        edge_dir, [decided if e.name == name else e for e in experiments]
    )
    return decided
