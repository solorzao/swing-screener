"""Proposals router (split from test_api.py): GET /api/proposals and the
approve/withdraw decisions."""

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swing_screener.config import StrategyConfig
from swing_screener.pipeline.proposed import (
    load_proposed_for,
    store_filename,
)
from swing_screener.pipeline.registry import Experiment
from tests.cockpit.conftest import (
    _HDR,
    _client,
    _proposal,
    _write_proposals,
)

# ---- proposals: GET /api/proposals + the approve/withdraw decisions ----

# The proposal wire form -- a closed set, like STAT_KEYS: the 8 stored
# ProposedVariant fields plus the three derived decision aids and the
# promotion checklist (populated on approved rows only, null otherwise).
PROPOSAL_KEYS = {"name", "play_type", "delta", "rationale", "hunch_ref", "status",
                 "drafted_at", "provenance", "gate_verdict", "delta_vs_incumbent",
                 "noop", "promotion_checklist"}

# Both decision responses carry this honesty line verbatim: decide_proposal edits
# the WORKING TREE only -- git capturing the flip is the human's move.
_NOTE = "uncommitted working-tree edit — commit with your decision"


def test_proposals_list_both_play_types_in_file_order(tmp_path: Path) -> None:
    """GET /api/proposals returns continuation then reversal, file order within;
    every row is the closed 12-key wire set with the 8 stored fields verbatim."""
    _write_proposals(tmp_path, "continuation",
                     [_proposal("c1", "continuation", {"max_extension_atr": 1.5})])
    _write_proposals(tmp_path, "reversal", [
        _proposal("r2", "reversal", {"reversal_confirm_window": 5}),
        _proposal("r1", "reversal", {"min_target_r": 2.0}),
    ])
    r = _client(tmp_path).get("/api/proposals")
    assert r.status_code == 200
    assert r.json()["store_errors"] == []  # both stores healthy
    rows = r.json()["proposals"]
    assert [(row["play_type"], row["name"]) for row in rows] == [
        ("continuation", "c1"), ("reversal", "r2"), ("reversal", "r1")]
    assert all(set(row) == PROPOSAL_KEYS for row in rows)
    c1 = rows[0]
    assert c1["delta"] == {"max_extension_atr": 1.5}
    assert c1["rationale"] == "hunch text"
    assert c1["hunch_ref"] == "reversal:2026-07-01:3"
    assert c1["status"] == "queued"
    assert c1["drafted_at"] == "2026-07-10"
    assert c1["provenance"] == "analyst:test"


def test_proposals_gate_verdict_and_noop(tmp_path: Path) -> None:
    """``gate_verdict`` runs the REAL gatekeeper per row: 'ok' for a legal delta,
    the ValueError text for an unknown knob or a frozen indicator period. ``noop``
    mirrors build_config_grid's incumbent-equality skip and never marks an invalid
    row (a row that fails the gate is invalid, not a no-op)."""
    _write_proposals(tmp_path, "reversal", [
        _proposal("legal", "reversal", {"max_extension_atr": 1.5}),
        _proposal("bogus", "reversal", {"no_such_knob": 1}),
        _proposal("frozen", "reversal", {"ema_fast": 10}),
        _proposal("same", "reversal",
                  {"max_extension_atr": StrategyConfig().max_extension_atr}),
    ])
    rows = {row["name"]: row for row in
            _client(tmp_path).get("/api/proposals").json()["proposals"]}
    assert rows["legal"]["gate_verdict"] == "ok"
    assert rows["legal"]["noop"] is False
    assert "no_such_knob" in rows["bogus"]["gate_verdict"]
    assert rows["bogus"]["noop"] is False
    assert "indicator field" in rows["frozen"]["gate_verdict"]
    assert rows["frozen"]["noop"] is False
    assert rows["same"]["gate_verdict"] == "ok"
    assert rows["same"]["noop"] is True


def test_proposals_delta_vs_incumbent(tmp_path: Path) -> None:
    """Each delta knob becomes {knob, current, proposed}: ``current`` is the
    incumbent StrategyConfig default; an unknown knob's current is null (the
    gate_verdict already names the error)."""
    _write_proposals(tmp_path, "continuation", [
        _proposal("mix", "continuation",
                  {"max_extension_atr": 1.5, "no_such_knob": 9}),
    ])
    row = _client(tmp_path).get("/api/proposals").json()["proposals"][0]
    assert row["delta_vs_incumbent"] == [
        {"knob": "max_extension_atr",
         "current": StrategyConfig().max_extension_atr, "proposed": 1.5},
        {"knob": "no_such_knob", "current": None, "proposed": 9},
    ]


def test_proposals_missing_files_are_empty_not_errors(tmp_path: Path) -> None:
    """No proposed.json anywhere -> empty rows AND empty store_errors (missing is
    'nothing queued', never corruption); one play type missing -> only the
    other's rows (a play type with nothing queued is the common case)."""
    client = _client(tmp_path)
    assert client.get("/api/proposals").json() == {
        "proposals": [], "store_errors": []}
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    body = client.get("/api/proposals").json()
    assert [row["name"] for row in body["proposals"]] == ["r1"]
    assert body["store_errors"] == []


# Both corruption flavors of a proposal store, keyed by which exception the load
# raises: malformed JSON (json.JSONDecodeError -- a ValueError SUBCLASS, the 409
# shadowing trap) and syntactically-valid JSON whose row ProposedVariant(**d)
# can't rebuild (TypeError).
_CORRUPT_STORES = [
    pytest.param("{not json", id="malformed-json"),
    pytest.param('[{"name": "x", "bogus_field": 1}]', id="mangled-row"),
]


@pytest.mark.parametrize("corrupt", _CORRUPT_STORES)
def test_proposals_get_corrupt_store_degrades_per_play_type(
    tmp_path: Path, corrupt: str,
) -> None:
    """One corrupt store never blanks the screen: the healthy play type still
    renders and ``store_errors`` names the broken one -- play-type name ONLY,
    never the parser message or a path."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    (tmp_path / store_filename("continuation")).write_text(
        corrupt, encoding="utf-8")
    r = _client(tmp_path).get("/api/proposals")
    assert r.status_code == 200
    body = r.json()
    assert [row["name"] for row in body["proposals"]] == ["r1"]
    assert body["store_errors"] == ["continuation"]
    assert tmp_path.name not in r.text  # leak posture holds on the degrade path


def test_proposals_get_wrong_typed_row_degrades_not_500(tmp_path: Path) -> None:
    """The THIRD corruption flavor, GET-only: right keys, wrong TYPES (e.g.
    ``"delta": 1.5``). ``load_proposed`` rebuilds the row fine (ProposedVariant
    does no type validation), so the failure fires later, in ``_proposal_row`` --
    which must therefore run INSIDE the try: the play type degrades into
    ``store_errors`` and the healthy one still renders, never a 500. NOT in
    _CORRUPT_STORES: POST behaves differently on this flavor (it 200s,
    defensibly -- decide_proposal reads only status/rationale)."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    # A HEALTHY row ahead of the wrong-typed one: rows must be built list-first
    # (extend-with-generator would append 'fine' before raising, leaking a
    # partial store onto the wire).
    wrong_types = (
        '[{"name": "fine", "play_type": "continuation", '
        '"delta": {"max_extension_atr": 1.2}, "rationale": "r", '
        '"hunch_ref": "h", "status": "queued", "drafted_at": "2026-07-10", '
        '"provenance": "p"},'
        ' {"name": "x", "play_type": "continuation", "delta": 1.5, '
        '"rationale": "r", "hunch_ref": "h", "status": "queued", '
        '"drafted_at": "2026-07-10", "provenance": "p"}]')
    (tmp_path / store_filename("continuation")).write_text(
        wrong_types, encoding="utf-8")
    r = _client(tmp_path).get("/api/proposals")
    assert r.status_code == 200
    body = r.json()
    assert [row["name"] for row in body["proposals"]] == ["r1"]  # no partial leak
    assert body["store_errors"] == ["continuation"]
    assert tmp_path.name not in r.text  # leak posture holds on the degrade path


@pytest.mark.parametrize("corrupt", _CORRUPT_STORES)
def test_proposal_decision_corrupt_store_is_503_never_409(
    tmp_path: Path, corrupt: str,
) -> None:
    """Both corruption flavors on POST are the FIXED 503 detail -- never a 409
    (json.JSONDecodeError IS a ValueError: mapped after the state-conflict arm, a
    typo'd store would read as 'already decided') and never a 500. The store's
    bytes are untouched: fixing the file by hand is the whole recovery path."""
    (tmp_path / store_filename("reversal")).write_text(corrupt, encoding="utf-8")
    client = _client(tmp_path)
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "x"}, headers=_HDR)
        assert r.status_code == 503
        assert r.json()["detail"] == (
            "proposal store unreadable -- fix edge/reversal.proposed.json by hand")
    stored = (tmp_path / store_filename("reversal")).read_text(encoding="utf-8")
    assert stored == corrupt


def test_promotion_checklist_names_real_registry_fields() -> None:
    """Drift guard: the checklist's registry-row line names Experiment fields
    (stopping rule, mde_r, target_ci_halfwidth_r, registered sha) as prose -- a
    registry field rename must break HERE, not silently rot the checklist text."""
    assert {"stopping_rule", "mde_r", "target_ci_halfwidth_r", "registered_sha"} <= {
        f.name for f in fields(Experiment)}


def test_approve_proposal_rewrites_the_store_with_the_checklist(
    tmp_path: Path,
) -> None:
    """Approve MARKS the row (status flip + rationale audit append, the store
    rewritten in place) and returns the verbatim three-artifact promotion
    checklist -- it never touches variants.py or experiments.json itself.
    ``file`` is the repo-relative label; the resolved edge dir never leaks."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    r = client.post("/api/proposals/reversal/r1/approve",
                    json={"reason": "worth a slot"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "r1"
    assert body["play_type"] == "reversal"
    assert body["status"] == "approved"
    assert body["file"] == "edge/reversal.proposed.json"
    assert body["note"] == _NOTE
    assert body["checklist"] == [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        ("2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)"),
        "3. edge/reversal.proposed.json — this flip (done)",
    ]
    # Leak posture: the resolved edge_dir (tmp_path) must not appear on the wire.
    assert tmp_path.name not in r.text
    (reloaded,) = load_proposed_for("reversal", tmp_path)
    assert reloaded.status == "approved"
    assert reloaded.rationale.endswith(
        f" APPROVED {datetime.now(UTC).date().isoformat()}: worth a slot")


def test_proposals_get_carries_the_checklist_on_approved_rows(
    tmp_path: Path,
) -> None:
    """The promotion checklist is RECOVERABLE: an approved row's GET carries the
    same verbatim three-artifact list the approve response built -- closing the
    approve-then-refresh trap, where the next steps lived only in the transient
    POST body. Queued and withdrawn rows carry null. The list is parameterized
    by the STORE's play type (the file the row was loaded from), so item 3
    names the right proposed.json per book."""
    _write_proposals(tmp_path, "continuation", [
        _proposal("c_ok", "continuation", {"max_extension_atr": 1.5},
                  status="approved"),
    ])
    _write_proposals(tmp_path, "reversal", [
        _proposal("r_q", "reversal", {"min_target_r": 2.0}),
        _proposal("r_ok", "reversal", {"min_target_r": 3.0},
                  status="approved"),
        _proposal("r_out", "reversal", {"min_target_r": 4.0},
                  status="withdrawn"),
    ])
    rows = {row["name"]: row for row in
            _client(tmp_path).get("/api/proposals").json()["proposals"]}
    assert rows["c_ok"]["promotion_checklist"] == [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        ("2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)"),
        "3. edge/continuation.proposed.json — this flip (done)",
    ]
    assert rows["r_ok"]["promotion_checklist"] == [
        "1. pipeline/variants.py — add the roster line (replace(base, **delta))",
        ("2. edge/experiments.json — add the registry row (stopping rule, mde_r, "
        "target_ci_halfwidth_r, registered sha)"),
        "3. edge/reversal.proposed.json — this flip (done)",
    ]
    assert rows["r_q"]["promotion_checklist"] is None
    assert rows["r_out"]["promotion_checklist"] is None


def test_withdraw_proposal_and_withdraw_after_approve(tmp_path: Path) -> None:
    """Withdraw works from queued AND from approved (an approval can be walked
    back); the response carries file + note but NO checklist, and the walked-back
    row keeps both audit appends."""
    _write_proposals(tmp_path, "continuation", [
        _proposal("direct", "continuation", {"max_extension_atr": 1.5}),
        _proposal("walked_back", "continuation", {"max_extension_atr": 1.0}),
    ])
    client = _client(tmp_path)
    r = client.post("/api/proposals/continuation/direct/withdraw",
                    json={"reason": "superseded"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "withdrawn"
    assert body["file"] == "edge/continuation.proposed.json"
    assert body["note"] == _NOTE
    assert "checklist" not in body
    ok = client.post("/api/proposals/continuation/walked_back/approve",
                     json={"reason": "test it"}, headers=_HDR)
    assert ok.status_code == 200
    r2 = client.post("/api/proposals/continuation/walked_back/withdraw",
                     json={"reason": "changed my mind"}, headers=_HDR)
    assert r2.status_code == 200
    assert r2.json()["status"] == "withdrawn"
    rows = {pv.name: pv for pv in load_proposed_for("continuation", tmp_path)}
    assert rows["direct"].status == "withdrawn"
    assert rows["walked_back"].status == "withdrawn"
    assert " APPROVED " in rows["walked_back"].rationale  # the audit trail survives
    assert " WITHDRAWN " in rows["walked_back"].rationale


def test_proposal_decision_404_on_unknown_name(tmp_path: Path) -> None:
    """An unknown name is decide_proposal's KeyError, surfaced as a 404 carrying
    the exception's own message -- unquoted (args[0], never str(KeyError)). A
    missing store file is the same 404 (load_proposed_for reads it as [])."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    r = client.post("/api/proposals/reversal/nope/approve",
                    json={"reason": "x"}, headers=_HDR)
    assert r.status_code == 404
    assert r.json()["detail"] == "no proposal named 'nope' for reversal"
    r2 = client.post("/api/proposals/continuation/ghost/withdraw",
                     json={"reason": "x"}, headers=_HDR)
    assert r2.status_code == 404


def test_proposal_decision_409_on_wrong_state(tmp_path: Path) -> None:
    """A refused transition is decide_proposal's ValueError, surfaced 409: approve
    is queued-only (re-approve refuses), and a withdrawn row refuses BOTH verbs
    (a withdrawal is final until a human hand-edits it). Refusals never write."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    assert client.post("/api/proposals/reversal/r1/approve",
                       json={"reason": "first"}, headers=_HDR).status_code == 200
    again = client.post("/api/proposals/reversal/r1/approve",
                        json={"reason": "second"}, headers=_HDR)
    assert again.status_code == 409
    assert "its status is 'approved'" in again.json()["detail"]
    assert client.post("/api/proposals/reversal/r1/withdraw",
                       json={"reason": "walk back"}, headers=_HDR).status_code == 200
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "again"}, headers=_HDR)
        assert r.status_code == 409
        assert "its status is 'withdrawn'" in r.json()["detail"]
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.rationale.count("WITHDRAWN") == 1  # refused transitions never write


def test_proposal_decisions_require_the_cockpit_header(tmp_path: Path) -> None:
    """Headerless approve/withdraw die at the guard (403) before any store read;
    the row stays queued."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    for action in ("approve", "withdraw"):
        r = client.post(f"/api/proposals/reversal/r1/{action}",
                        json={"reason": "x"})
        assert r.status_code == 403
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.status == "queued"


@pytest.mark.parametrize("url,body", [
    ("/api/proposals/reversal/r1/approve", {"reason": ""}),
    ("/api/proposals/reversal/r1/approve", {"reason": "   "}),
    ("/api/proposals/reversal/r1/withdraw", {"reason": "x" * 201}),
    ("/api/proposals/reversal/r1/approve", {}),
    ("/api/proposals/daytrade/r1/approve", {"reason": "x"}),
])
def test_proposal_decision_validation(tmp_path: Path, url: str,
                                      body: dict[str, object]) -> None:
    """Blank/missing/overlong reason and an unknown play_type are 422s (the model's
    strip + bounds; the Literal path param); none of them reach the store."""
    _write_proposals(tmp_path, "reversal",
                     [_proposal("r1", "reversal", {"max_extension_atr": 1.5})])
    client = _client(tmp_path)
    assert client.post(url, json=body, headers=_HDR).status_code == 422
    (row,) = load_proposed_for("reversal", tmp_path)
    assert row.status == "queued"


