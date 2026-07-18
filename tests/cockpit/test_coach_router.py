"""Coach router contract: reads scoped to personal books, the confirm-tag gate, and
the header guard on writes."""

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from swing_screener.cockpit.api import create_app
from swing_screener.db.models import JournalReview, JournalTradeTag, WeaknessesProfile
from swing_screener.db.session import get_engine

_HDR = {"X-Cockpit": "1"}


def _app(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'c.db').as_posix()}"
    engine = get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path)), engine


def _review(engine, *, trade_id=1, proposals=None):
    facts = {"result": 2.0, "outcome": "target", "tag_proposals": proposals or []}
    with Session(engine) as s:
        r = JournalReview(identity_key=f"trade_close:manual_equity:{trade_id}",
                          kind="trade_close", book="manual_equity", trade_id=trade_id,
                          facts_json=json.dumps(facts), source="analyst", narrative="draft",
                          generated_at=datetime(2026, 7, 12, 10, 0))
        s.add(r)
        s.commit()
        return r.id


def test_reviews_scoped_to_personal_book(tmp_path: Path):
    client, engine = _app(tmp_path)
    _review(engine)
    r = client.get("/api/coach/reviews?book=manual_equity")
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["facts"]["outcome"] == "target"
    # a machine book is rejected -- the Coach voice never applies to it
    assert client.get("/api/coach/reviews?book=research").status_code == 422


def test_edit_requires_header_and_persists(tmp_path: Path):
    client, engine = _app(tmp_path)
    rid = _review(engine)
    assert client.post(f"/api/coach/reviews/{rid}/edit",
                       json={"human_edit": "my take"}).status_code == 403  # no header
    ok = client.post(f"/api/coach/reviews/{rid}/edit",
                     json={"human_edit": "my take"}, headers=_HDR)
    assert ok.status_code == 200 and ok.json()["human_edit"] == "my take"


def test_confirm_tag_applies_only_a_parked_proposal(tmp_path: Path):
    client, engine = _app(tmp_path)
    rid = _review(engine, proposals=[{"name": "moved_stop", "kind": "mistake",
                                      "reason": "x"}])
    # a proposal that wasn't parked is rejected
    assert client.post(f"/api/coach/reviews/{rid}/confirm-tag",
                       json={"name": "chased", "kind": "mistake"},
                       headers=_HDR).status_code == 422
    ok = client.post(f"/api/coach/reviews/{rid}/confirm-tag",
                     json={"name": "moved_stop", "kind": "mistake"}, headers=_HDR)
    assert ok.status_code == 200
    with Session(engine) as s:
        link = s.query(JournalTradeTag).filter_by(book="manual_equity", trade_id=1).one()
        assert link.source == "analyst"      # confirmed -> analyst provenance


def test_one_corrupt_facts_row_never_500s_the_list(tmp_path: Path):
    """Per-row degrade (the cockpit posture): one corrupt ``facts_json`` row is
    SKIPPED -- the list still serves every parseable review, never a 500."""
    client, engine = _app(tmp_path)
    good = _review(engine)
    with Session(engine) as s:
        s.add(JournalReview(identity_key="trade_close:manual_equity:2",
                            kind="trade_close", book="manual_equity", trade_id=2,
                            facts_json="{not json", source="analyst",
                            generated_at=datetime(2026, 7, 12, 11, 0)))
        s.commit()
    r = client.get("/api/coach/reviews?book=manual_equity")
    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == [good]  # the corrupt row is skipped


def test_weaknesses_returns_latest_or_empty(tmp_path: Path):
    client, engine = _app(tmp_path)
    assert client.get("/api/coach/weaknesses").json()["n_reviews"] == 0
    with Session(engine) as s:
        s.add(WeaknessesProfile(scope="personal",
                                items_json=json.dumps({"items": [{"weakness": "exits early"}],
                                                       "thin_data": True, "n_reviews": 3}),
                                generated_at=datetime(2026, 7, 12, 12, 0)))
        s.commit()
    body = client.get("/api/coach/weaknesses").json()
    assert body["items"][0]["weakness"] == "exits early" and body["generated_at"]
