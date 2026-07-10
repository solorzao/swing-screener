"""Machine-readable experiment pre-registration (edge/experiments.json).

An Experiment row is the settlement card's source of truth: the stopping rule renders
VERBATIM with the sha it was registered under. Fields are never inferred at render time
-- a rule invented when the card is drawn is pre-registration theater, the exact failure
this file exists to prevent. Legacy experiments carry provenance describing where the
original wording lived (roster comment / study doc) and the sha that introduced the
roster line.
"""

import json
from dataclasses import asdict, dataclass
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
