"""Playbooks router (split from test_api.py): /api/playbooks (verdicts, md
drift, degrade postures) and /api/attention."""

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from swing_screener.analytics.calibration import MIN_LEADERBOARD_N
from swing_screener.pipeline.proposed import (
    store_filename,
)
from swing_screener.pipeline.reflect import (
    Verdict,
    render_edge_file,
    verdicts_to_json,
)
from tests.cockpit.conftest import (
    _analysis_row,
    _book_trade,
    _client,
    _client_and_engine,
    _experiment,
    _proposal,
    _write_proposals,
    _write_registry,
)

# ---- Task 11 reads: /api/playbooks, /api/weather, /api/analyst, /api/attention


PLAYBOOKS_KEYS = {"books", "due_play_types", "store_errors", "ci_note"}
BOOK_KEYS = {"play_type", "md", "md_error", "frontmatter", "verdicts",
             "verdicts_error", "drift", "falsified", "reflection_due"}
# The verdict wire row: EVERY Verdict field by name, a closed set like STAT_KEYS
# -- deliberately NOT a Stat dict (tier rides verdict rows; scope decision 11),
# and deliberately NO ci_high (the wire's ci_note says why).
VERDICT_ROW_KEYS = {"play_type", "dimension", "bucket", "tier", "n",
                    "expectancy_r", "ci_low", "n_clusters", "source",
                    "n_forward", "n_replay", "cost_level", "corpus_id"}
ATTENTION_KEYS = {"proposals_queued", "proposals_approved_pending",
                  "reflection_due", "latest_analysis_id", "audit_unacked",
                  "audit_worst", "coach_pending", "research_prs"}
def _sidecar_verdict(**over: object) -> Verdict:
    base: dict = {
        "play_type": "reversal", "dimension": "market_trend", "bucket": "bear",
        "tier": "replay_screened", "n": 2387, "expectancy_r": 0.196, "ci_low": 0.104,
        "n_clusters": 100, "source": "replay",
    }
    base.update(over)
    return Verdict(**base)


def test_playbooks_verdicts_carry_cost_corpus(tmp_path: Path) -> None:
    """Verdict wire rows are the SIDECAR verbatim: the closed 13-key set with the
    provenance stamps riding along. A stamped row serves cost_level/corpus_id
    verbatim; a PRE-Phase-3 row (both keys ABSENT from the committed JSON)
    serves an explicit None for each -- backfilling any default there is the
    fabricated-provenance mutation this test exists to kill. The hunch-display
    fix's ``n_forward``/``n_replay`` book depths ride verbatim too; a row that
    predates THOSE keys serves the loader's own 0 (the reflection's "no recorded
    depth" convention, same for every consumer). ``ci_note`` states the bound's
    meaning on the wire (Bonferroni-corrected lower, no ci_high)."""
    stamped = _sidecar_verdict(cost_level="0.05",
                               corpus_id="corpus sha=abc as_of=20260705",
                               n_forward=1, n_replay=2387)
    legacy = {  # a committed pre-Phase-3 sidecar row: NO cost/corpus/depth keys
        "play_type": "reversal", "dimension": "volatility_tier", "bucket": "low",
        "tier": "hunch", "n": 1950, "expectancy_r": 0.0506, "ci_low": -0.0568,
        "n_clusters": 72, "source": "none",
    }
    rows_json = json.loads(verdicts_to_json([stamped])) + [legacy]
    (tmp_path / "reversal.verdicts.json").write_text(
        json.dumps(rows_json), encoding="utf-8")
    r = _client(tmp_path).get("/api/playbooks")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == PLAYBOOKS_KEYS
    assert "Bonferroni" in body["ci_note"] and "no ci_high" in body["ci_note"]
    books = {b["play_type"]: b for b in body["books"]}
    assert set(books) == {"continuation", "reversal"}
    assert all(set(b) == BOOK_KEYS for b in body["books"])
    rows = books["reversal"]["verdicts"]
    assert [set(row) for row in rows] == [VERDICT_ROW_KEYS] * 2
    assert rows[0]["cost_level"] == "0.05"
    assert rows[0]["corpus_id"] == "corpus sha=abc as_of=20260705"
    assert rows[0]["n_forward"] == 1 and rows[0]["n_replay"] == 2387
    assert rows[1]["cost_level"] is None  # served None, never fabricated
    assert rows[1]["corpus_id"] is None
    assert rows[1]["n_forward"] == 0 and rows[1]["n_replay"] == 0
    assert rows[1]["n"] == 1950 and rows[1]["ci_low"] == -0.0568


def test_playbooks_md_verbatim_drift_lamp_and_falsified(tmp_path: Path) -> None:
    """The md is served VERBATIM (prose only -- numbers come from the sidecar),
    the drift lamp is the structural md-vs-sidecar check, and the Falsified body
    rides pre-extracted through the reflection's own parser. Continuation is
    faithful -> drift ok; reversal's md files the sidecar's screened condition
    under Hunches (the stale-verdicts hazard, exactly) -> that token flags."""
    cont = _sidecar_verdict(play_type="continuation")
    cont_md = render_edge_file("continuation", "thesis", [cont], n_closed_now=7,
                               prior_falsified="- old claim REFUTED 2026-07-03")
    (tmp_path / "continuation.md").write_text(cont_md, encoding="utf-8")
    (tmp_path / "continuation.verdicts.json").write_text(
        verdicts_to_json([cont]), encoding="utf-8")

    rev = _sidecar_verdict()
    stale_md = render_edge_file(
        "reversal", "thesis", [dataclasses.replace(rev, tier="hunch")],
        n_closed_now=3)
    (tmp_path / "reversal.md").write_text(stale_md, encoding="utf-8")
    (tmp_path / "reversal.verdicts.json").write_text(
        verdicts_to_json([rev]), encoding="utf-8")

    books = {b["play_type"]: b for b in
             _client(tmp_path).get("/api/playbooks").json()["books"]}
    c = books["continuation"]
    assert c["md"] == cont_md  # verbatim, never re-rendered
    assert c["frontmatter"] == {"forward_closed_at_last_reflection": 7,
                                "last_reflected": None}
    assert c["drift"] == {"ok": True, "missing": []}
    assert "old claim REFUTED" in c["falsified"]
    r = books["reversal"]
    assert r["md"] == stale_md
    assert r["drift"] == {"ok": False, "missing": [
        {"token": "market_trend=bear", "tier": "replay_screened"}]}


def test_playbooks_degrades_per_book_and_drift_never_fabricates_ok(
    tmp_path: Path,
) -> None:
    """A corrupt sidecar degrades ITS book only: verdicts empty, the error names
    the exception CLASS only (never the parser message), top-level store_errors
    names the play type (the proposals-GET precedent), and drift is null --
    explicitly UNKNOWN, never a fabricated ok. The healthy book still renders."""
    good = _sidecar_verdict(play_type="continuation")
    (tmp_path / "continuation.verdicts.json").write_text(
        verdicts_to_json([good]), encoding="utf-8")
    (tmp_path / "reversal.verdicts.json").write_text(
        '{"oops": "truncated', encoding="utf-8")
    r = _client(tmp_path).get("/api/playbooks")
    assert r.status_code == 200
    body = r.json()
    assert body["store_errors"] == ["reversal"]
    books = {b["play_type"]: b for b in body["books"]}
    assert books["continuation"]["verdicts_error"] is None
    assert len(books["continuation"]["verdicts"]) == 1
    rev = books["reversal"]
    assert rev["verdicts"] == []
    assert rev["verdicts_error"] == "unreadable (JSONDecodeError)"  # class only
    assert rev["drift"] is None  # unknown -- a drift lamp must not read ok here


def test_playbooks_unreadable_md_degrades_that_book_only(tmp_path: Path) -> None:
    """A non-UTF-8 md (a cp1252 smart-quote from a Windows hand-edit -- the
    drift lamp's OWN stated threat model) must not 500 either surface.
    ``reflect._edge_text`` stays strict by decision (the nightly reflection
    fails loudly on corruption); the ENDPOINT degrades: that book serves md ""
    + ``md_error`` naming the class only, drift null (faithfulness against
    nothing is unknowable), while its SIDECAR still renders -- the numbers
    never depended on the prose. The other book is untouched. /api/attention
    -- whose due gate reads both mds -- stays 200 with reflection_due degraded
    quietly to [] (md_error here is the loud marker)."""
    cont = _sidecar_verdict(play_type="continuation")
    cont_md = render_edge_file("continuation", "thesis", [cont], n_closed_now=7)
    (tmp_path / "continuation.md").write_text(cont_md, encoding="utf-8")
    (tmp_path / "continuation.verdicts.json").write_text(
        verdicts_to_json([cont]), encoding="utf-8")
    (tmp_path / "reversal.verdicts.json").write_text(
        verdicts_to_json([_sidecar_verdict()]), encoding="utf-8")
    (tmp_path / "reversal.md").write_bytes(  # \x93/\x94 = cp1252 smart quotes
        b"## Thesis\n\x93hand-edited on Windows\x94\n")

    client = _client(tmp_path)
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    body = r.json()
    books = {b["play_type"]: b for b in body["books"]}
    bad = books["reversal"]
    assert bad["md"] == ""
    assert bad["md_error"] == "unreadable (UnicodeDecodeError)"  # class only
    assert bad["drift"] is None  # unknown, never a fabricated ok
    assert len(bad["verdicts"]) == 1  # the sidecar still renders
    assert bad["verdicts_error"] is None
    good = books["continuation"]
    assert good["md_error"] is None
    assert good["md"] == cont_md
    assert good["drift"] == {"ok": True, "missing": []}
    assert body["due_play_types"] == []  # the due gate read the bad md: quiet

    a = client.get("/api/attention")
    assert a.status_code == 200
    assert a.json()["reflection_due"] == []


def test_playbooks_mangled_tier_row_degrades_not_500(tmp_path: Path) -> None:
    """A hand-mangled sidecar row whose values PASS ``Verdict(**d)`` but break
    the drift check (an unhashable list tier) is the same corrupt-store state:
    the drift call lives INSIDE the row-build umbrella, so the book reads
    'unreadable (TypeError)' + drift null -- never a 500."""
    row = {"play_type": "reversal", "dimension": "market_trend",
           "bucket": "bear", "tier": ["hunch"], "n": 1, "expectancy_r": 0.1,
           "ci_low": -0.1, "n_clusters": 1, "source": "none"}
    (tmp_path / "reversal.verdicts.json").write_text(
        json.dumps([row]), encoding="utf-8")
    r = _client(tmp_path).get("/api/playbooks")
    assert r.status_code == 200
    body = r.json()
    assert body["store_errors"] == ["reversal"]
    rev = {b["play_type"]: b for b in body["books"]}["reversal"]
    assert rev["verdicts"] == []  # never a half-built row list on the wire
    assert rev["verdicts_error"] == "unreadable (TypeError)"
    assert rev["drift"] is None


def test_playbooks_empty_edge_dir_is_a_setup_state(tmp_path: Path) -> None:
    """A fresh clone (no md, no sidecars, empty DB) is a SETUP state, not an
    error: md "", verdicts_error 'missing' (distinct from corrupt -- it never
    lands in store_errors), drift null (unknown), nothing due."""
    body = _client(tmp_path).get("/api/playbooks").json()
    assert body["store_errors"] == []
    assert body["due_play_types"] == []
    for b in body["books"]:
        assert b["md"] == ""
        assert b["md_error"] is None  # missing is a setup state, not a failure
        assert b["verdicts"] == []
        assert b["verdicts_error"] == "missing"
        assert b["drift"] is None
        assert b["falsified"] == ""
        assert b["frontmatter"] == {"forward_closed_at_last_reflection": 0,
                                    "last_reflected": None}
        assert b["reflection_due"] is False


def test_playbooks_reflection_due_mirrors_the_reflection_gate(
    tmp_path: Path,
) -> None:
    """``reflection_due`` comes from ``due_play_types`` -- the reflection's OWN
    re-arm gate (>= trigger new closes at the baseline/default forward facet
    since the frontmatter counter; the trigger is pinned to MIN_LEADERBOARD_N).
    Twenty closed reversal rows against a missing-file counter of 0 arm
    reversal; continuation stays quiet; /api/attention agrees (same loader)."""
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        for i in range(MIN_LEADERBOARD_N):
            s.add(_book_trade(f"T{i}", 0.5))
        s.commit()
    body = client.get("/api/playbooks").json()
    books = {b["play_type"]: b for b in body["books"]}
    assert body["due_play_types"] == ["reversal"]
    assert books["reversal"]["reflection_due"] is True
    assert books["continuation"]["reflection_due"] is False
    assert client.get("/api/attention").json()["reflection_due"] == ["reversal"]


def test_attention_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The permanent-poll strip feed's EXACT eight-key shape. Queued and
    approved-pending are DISTINCT lists -- an approval only MARKS, so the strip
    must keep showing it until a human promotes (the registry cross-check,
    tested below); a withdrawn row appears in neither. A corrupt store degrades
    QUIETLY (its names absent, still 200 -- the loud marker lives on
    /api/proposals); ``latest_analysis_id`` is the max id or null. The empty DB
    is all-quiet: zero counts, null worst, empty PR list (poller unconfigured
    -> honest [], never an error)."""
    monkeypatch.delenv("SWING_GH_TOKEN", raising=False)
    monkeypatch.delenv("SWING_GH_REPO", raising=False)
    client, engine = _client_and_engine(tmp_path)
    empty = client.get("/api/attention")
    assert empty.status_code == 200
    assert empty.json() == {"proposals_queued": [],
                            "proposals_approved_pending": [],
                            "reflection_due": [], "latest_analysis_id": None,
                            "audit_unacked": 0, "audit_worst": None,
                            "coach_pending": 0, "research_prs": []}

    _write_proposals(tmp_path, "reversal", [
        _proposal("r_q", "reversal", {"max_extension_atr": 1.5}),
        _proposal("r_ok", "reversal", {"min_target_r": 2.0}, status="approved"),
        _proposal("r_out", "reversal", {"min_target_r": 3.0},
                  status="withdrawn"),
    ])
    (tmp_path / store_filename("continuation")).write_text(
        "{broken", encoding="utf-8")
    with Session(engine) as s:
        s.add(_analysis_row(ticker="AMD"))
        newest = _analysis_row(ticker="NVDA")
        s.add(newest)
        s.commit()
        latest_id = newest.id
    body = client.get("/api/attention").json()
    assert set(body) == ATTENTION_KEYS
    assert body["proposals_queued"] == ["r_q"]
    assert body["proposals_approved_pending"] == ["r_ok"]  # distinct, visible
    assert body["reflection_due"] == []
    assert body["latest_analysis_id"] == latest_id


def test_attention_approved_pending_clears_once_promoted(tmp_path: Path) -> None:
    """The '· promote' nag must END when the promotion commit lands: an approved
    proposal whose name has an edge/experiments.json registry row (the loader the
    forward-books surface already uses) has been promoted and leaves
    ``proposals_approved_pending`` -- a RETIRED registry row still counts (the
    experiment existed; retiring is not un-promoting). An approved name with no
    registry row keeps nagging, and an UNREADABLE registry excludes nothing (a
    persistent nag beats a silently vanished one)."""
    _write_proposals(tmp_path, "reversal", [
        _proposal("promoted_v1", "reversal", {"min_target_r": 2.0},
                  status="approved"),
        _proposal("retired_v1", "reversal", {"min_target_r": 3.0},
                  status="approved"),
        _proposal("still_waiting", "reversal", {"max_extension_atr": 1.5},
                  status="approved"),
    ])
    _write_registry(tmp_path, [
        _experiment("promoted_v1", kind="variant"),
        _experiment("retired_v1", kind="variant", status="retired",
                    decision="no edge"),
    ])
    client = _client(tmp_path)
    body = client.get("/api/attention").json()
    assert body["proposals_approved_pending"] == ["still_waiting"]

    (tmp_path / "experiments.json").write_text("{not json", encoding="utf-8")
    nagging = client.get("/api/attention").json()["proposals_approved_pending"]
    assert nagging == ["promoted_v1", "retired_v1", "still_waiting"]


def test_attention_audit_counts_unacked_and_worst_severity(tmp_path: Path) -> None:
    """``audit_unacked`` counts EVERY SystemAudit row awaiting the human ack --
    weekly reports and breaches alike (both are ack-able; the audit router's own
    semantics) -- and ``audit_worst`` is the gravest unacked severity (info <
    warn < alert). An acked alert contributes to neither: acking the only alert
    demotes worst to the surviving warn, and acking everything reads 0/null."""
    from swing_screener.db.models import SystemAudit

    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 7, 6),
                          period_to=date(2026, 7, 12), severity="info"))
        s.add(SystemAudit(kind="weekly", period_from=date(2026, 7, 13),
                          period_to=date(2026, 7, 19), severity="warn"))
        s.add(SystemAudit(kind="breach", period_from=date(2026, 7, 8),
                          period_to=date(2026, 7, 8), breach_key="cap:2026-07-08",
                          severity="alert"))
        s.add(SystemAudit(kind="breach", period_from=date(2026, 7, 9),
                          period_to=date(2026, 7, 9), breach_key="cap:2026-07-09",
                          severity="alert", acknowledged_by_human=True))
        s.commit()
    body = client.get("/api/attention").json()
    assert body["audit_unacked"] == 3
    assert body["audit_worst"] == "alert"
    with Session(engine) as s:
        for row in s.query(SystemAudit).filter_by(severity="alert").all():
            row.acknowledged_by_human = True
        s.commit()
    body = client.get("/api/attention").json()
    assert body["audit_unacked"] == 2
    assert body["audit_worst"] == "warn"  # the acked alert no longer dominates
    with Session(engine) as s:
        for row in s.query(SystemAudit).all():
            row.acknowledged_by_human = True
        s.commit()
    body = client.get("/api/attention").json()
    assert body["audit_unacked"] == 0
    assert body["audit_worst"] is None


def test_attention_coach_pending_counts_unconfirmed_proposal_reviews(
    tmp_path: Path,
) -> None:
    """``coach_pending`` counts per-trade Coach reviews with PARKED auto-tag
    proposals whose trade has no analyst-source overlay tag yet -- confirming
    writes the tag but leaves facts_json untouched, so the tag row is the only
    durable 'handled' evidence. Facts are serialized exactly as the writer does
    (``json.dumps`` in routers/trades.py -- the LIKE the endpoint matches). Not
    pending: an empty proposal list, a weekly rollup (no trade to tag), a review
    whose trade already carries an analyst tag. A HUMAN-sourced tag is not a
    confirm (the gate writes source='analyst')."""
    from swing_screener.db.models import JournalReview, JournalTag, JournalTradeTag

    def _facts(proposals: list[dict[str, str]]) -> str:
        return json.dumps({"realized_r": 1.2, "tag_proposals": proposals})

    parked = [{"name": "moved_stop", "kind": "mistake", "reason": "r"}]
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        tag = JournalTag(kind="mistake", name="moved_stop")
        s.add(tag)
        s.flush()
        s.add(JournalReview(identity_key="trade_close:manual_equity:1",
                            kind="trade_close", book="manual_equity", trade_id=1,
                            facts_json=_facts(parked), source="analyst"))
        # a HUMAN tag on trade 1 is not the confirm gate's write: still pending
        s.add(JournalTradeTag(trade_id=1, book="manual_equity", tag_id=tag.id,
                              source="human"))
        s.add(JournalReview(identity_key="trade_close:manual_equity:2",
                            kind="trade_close", book="manual_equity", trade_id=2,
                            facts_json=_facts(parked), source="analyst"))
        s.add(JournalTradeTag(trade_id=2, book="manual_equity", tag_id=tag.id,
                              source="analyst"))  # confirmed: not pending
        s.add(JournalReview(identity_key="trade_close:manual_equity:3",
                            kind="trade_close", book="manual_equity", trade_id=3,
                            facts_json=_facts([]), source="analyst"))  # to plan
        s.add(JournalReview(identity_key="weekly_rollup:manual_equity:a:b",
                            kind="weekly_rollup", book="manual_equity",
                            trade_id=None, facts_json=_facts(parked),
                            source="analyst"))  # rollup: nothing to confirm
        s.commit()
    assert client.get("/api/attention").json()["coach_pending"] == 1


def test_attention_research_prs_ride_the_gh_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``research_prs`` is ``gh.open_research_prs``'s answer verbatim -- the
    reflection/optimizer PRs that otherwise have zero cockpit surface. The
    endpoint adds nothing and reorders nothing (the gh module owns matching,
    caching, and the honest-[] failure posture, tested in test_gh.py)."""
    prs = [{"title": "reflection: update edge playbooks from the forward book",
            "url": "https://github.com/o/r/pull/7", "kind": "reflection"},
           {"title": "optimizer: raise max_extension_atr",
            "url": "https://github.com/o/r/pull/8", "kind": "optimizer"}]
    monkeypatch.setattr(
        "swing_screener.cockpit.routers.playbooks.open_research_prs", lambda: prs)
    body = _client(tmp_path).get("/api/attention").json()
    assert body["research_prs"] == prs
