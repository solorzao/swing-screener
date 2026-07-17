"""POST /api/experiments/{name}/decide -- the settlement decide action: marks
the registry (working-tree edit), hands back the roster checklist, guards its
preconditions loudly."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from swing_screener.cockpit.api import create_app
from swing_screener.db.session import get_engine
from swing_screener.pipeline.registry import load_experiments

_HDR = {"X-Cockpit": "1"}


def _experiment_row(name: str = "be_1r", kind: str = "arm") -> dict[str, object]:
    return {
        "name": name, "kind": kind, "play_type": "all",
        "control": "baseline" if kind == "arm" else "default",
        "hypothesis": "h", "stopping_rule": "rule verbatim",
        "mde_r": 0.1, "target_ci_halfwidth_r": 0.1,
        "registered_at": "2026-07-12", "registered_sha": "c2662eb432",
        "doc_ref": "doc", "provenance": "prov",
        "status": "active", "decided_at": None, "decision": None,
    }


def _client(tmp_path: Path, rows: list[dict[str, object]]) -> TestClient:
    (tmp_path / "experiments.json").write_text(
        json.dumps(rows, indent=2) + "\n", encoding="utf-8"
    )
    url = f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"
    get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path))


def test_decide_requires_cockpit_header(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row()])
    r = client.post("/api/experiments/be_1r/decide", json={"reason": "futile"})
    assert r.status_code == 403


def test_decide_unknown_name_404(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row()])
    r = client.post(
        "/api/experiments/nope/decide", json={"reason": "x"}, headers=_HDR
    )
    assert r.status_code == 404


def test_decide_blank_reason_422(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row()])
    r = client.post(
        "/api/experiments/be_1r/decide", json={"reason": ""}, headers=_HDR
    )
    assert r.status_code == 422


def test_decide_marks_registry_and_hands_roster_checklist(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row(), _experiment_row("v1", "variant")])
    r = client.post(
        "/api/experiments/be_1r/decide",
        json={"reason": "futile at n=30 -- retire"},
        headers=_HDR,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "retired"
    assert body["decided_at"] is not None
    # The checklist names the ARM roster for an arm and keeps the flip marked done.
    assert "pipeline/arms.py" in body["checklist"][0]
    assert "(done)" in body["checklist"][1]
    # The registry file itself carries the flip -- an uncommitted working-tree edit.
    rows = load_experiments(tmp_path)
    by_name = {e.name: e for e in rows}
    assert by_name["be_1r"].status == "retired"
    assert by_name["be_1r"].decision == "futile at n=30 -- retire"
    assert by_name["v1"].status == "active"  # untouched sibling
    # The forward wall now renders the card retired (no more decide nag).
    cards = client.get("/api/forward-books").json()["cards"]
    states = {c["name"]: c["state"] for c in cards}
    assert states["be_1r"] == "retired"


def test_decide_variant_checklist_names_variants_roster(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row("cont_volband", "variant")])
    r = client.post(
        "/api/experiments/cont_volband/decide",
        json={"reason": "absorbed"}, headers=_HDR,
    )
    assert r.status_code == 200
    assert "pipeline/variants.py" in r.json()["checklist"][0]


def test_decide_twice_409(tmp_path: Path) -> None:
    client = _client(tmp_path, [_experiment_row()])
    first = client.post(
        "/api/experiments/be_1r/decide", json={"reason": "futile"}, headers=_HDR
    )
    assert first.status_code == 200
    again = client.post(
        "/api/experiments/be_1r/decide", json={"reason": "again"}, headers=_HDR
    )
    assert again.status_code == 409
    assert "already retired" in again.json()["detail"]
