"""Weaknesses Profile builder: recurring flags become items with (book, trade_id)
evidence; sparse corpora are stamped thin_data; the row is staleness-stamped."""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import JournalReview
from swing_screener.db.session import get_engine
from swing_screener.journal.weaknesses import build_profile

_NOW = datetime(2026, 7, 12, 14, 0)


def _review(trade_id, facts, book="manual_equity"):
    return JournalReview(
        identity_key=f"trade_close:{book}:{trade_id}", kind="trade_close",
        book=book, trade_id=trade_id, facts_json=json.dumps(facts), source="analyst",
        generated_at=datetime(2026, 7, 10, 10, 0),
    )


def test_recurring_moved_stop_becomes_a_weakness_with_pair_evidence():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            _review(1, {"moved_stop": True, "outcome": "stop", "result": -0.8}),
            _review(2, {"moved_stop": True, "outcome": "stop", "result": -1.0}),
            _review(3, {"moved_stop": False, "outcome": "target", "result": 2.0}),
        ])
        s.commit()
        p = build_profile(s, now=_NOW, thin_floor=1)
        payload = json.loads(p.items_json)
        labels = {i["weakness"] for i in payload["items"]}
        assert "moves stops under heat" in labels
        moved = next(i for i in payload["items"] if i["weakness"] == "moves stops under heat")
        assert moved["count"] == 2
        assert {"book": "manual_equity", "trade_id": 1} in moved["evidence"]
        assert p.generated_at == _NOW            # staleness stamp


def test_single_occurrence_is_not_yet_a_weakness():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_review(1, {"moved_stop": True, "outcome": "stop", "result": -0.8}))
        s.commit()
        p = build_profile(s, now=_NOW, min_occurrences=2, thin_floor=1)
        assert json.loads(p.items_json)["items"] == []


def test_thin_data_flag_below_floor():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_review(1, {"moved_stop": True}))
        s.commit()
        p = build_profile(s, now=_NOW, thin_floor=5)
        payload = json.loads(p.items_json)
        assert payload["thin_data"] is True
        assert payload["n_reviews"] == 1


def test_empty_reviews_produces_a_thin_profile_not_a_crash():
    with Session(get_engine("sqlite:///:memory:")) as s:
        p = build_profile(s, now=_NOW)
        payload = json.loads(p.items_json)
        assert payload["items"] == [] and payload["thin_data"] is True
        assert p.covered_to == _NOW.date()
