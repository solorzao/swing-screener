"""Coach worker: drains the on-close draft queue, backfills narratives, honors the
spend cap, and never lets one bad row abort the batch. No real API is hit."""

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import CoachDraftRequest, JournalReview
from swing_screener.db.repo import create_coach_draft_request
from swing_screener.db.session import get_engine
from swing_screener.journal import coach_run
from swing_screener.settings import load_settings

_NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)

_FACTS = {"book": "manual_equity", "symbol": "AMD", "unit": "R", "result": 2.0,
          "outcome": "target", "hold_days": 4, "moved_stop": False, "override": None,
          "emotional_state": None, "exit_reason": "target", "mae_r": None, "mfe_r": None}


class _Usage:
    def __init__(self):
        self.input_tokens = 100
        self.output_tokens = 50


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.last_kwargs: dict = {}
        self.messages = self
    def create(self, **kw):
        self.last_kwargs = kw
        return _Resp(self._text)


def _settings(monkeypatch, *, enabled, max_usd=None):
    for k in ("SWING_COACH_ENABLED", "SWING_COACH_MAX_USD"):
        monkeypatch.delenv(k, raising=False)
    if enabled:
        monkeypatch.setenv("SWING_COACH_ENABLED", "1")
    if max_usd is not None:
        monkeypatch.setenv("SWING_COACH_MAX_USD", str(max_usd))
    return load_settings()


def _seed_review(s) -> JournalReview:
    r = JournalReview(identity_key="trade_close:manual_equity:1", kind="trade_close",
                      book="manual_equity", trade_id=1, facts_json=json.dumps(_FACTS),
                      source="analyst")
    s.add(r)
    s.commit()
    s.refresh(r)
    create_coach_draft_request(s, review_id=r.id, requested_at=_NOW)
    return r


def test_disabled_coach_backfills_template_narrative(monkeypatch):
    with Session(get_engine("sqlite:///:memory:")) as s:
        r = _seed_review(s)
        n = coach_run.process_pending(s, settings=_settings(monkeypatch, enabled=False), now=_NOW)
        assert n == 1
        s.refresh(r)
        assert r.narrative is not None and "AMD" in r.narrative
        assert r.est_cost_usd is None                       # no LLM
        assert s.query(CoachDraftRequest).one().status == "done"


def test_enabled_coach_uses_client_prose_and_records_usage(monkeypatch):
    with Session(get_engine("sqlite:///:memory:")) as s:
        r = _seed_review(s)
        client = _FakeClient("You reached target cleanly; consider a runner next time.")
        coach_run.process_pending(
            s, settings=_settings(monkeypatch, enabled=True), now=_NOW, client=client)
        s.refresh(r)
        assert r.narrative.startswith("You reached target")
        # Provenance unification (2026-07-17 audit): the stamped model column, the model
        # the API call was actually made with, and the worker's ONE constant all agree --
        # a future model change can never misattribute a review's spend.
        assert r.model == coach_run._MODEL == "claude-haiku-4-5"
        assert client.last_kwargs["model"] == coach_run._MODEL
        assert r.input_tokens == 100 and r.est_cost_usd is not None and r.est_cost_usd > 0


def test_over_budget_falls_back_to_template(monkeypatch):
    with Session(get_engine("sqlite:///:memory:")) as s:
        r = _seed_review(s)
        # zero ceiling -> already over budget on the first row -> no LLM call
        coach_run.process_pending(
            s, settings=_settings(monkeypatch, enabled=True, max_usd=0.0), now=_NOW,
            client=_FakeClient("SHOULD NOT BE USED"))
        s.refresh(r)
        assert "SHOULD NOT BE USED" not in (r.narrative or "")
        assert r.est_cost_usd is None


def test_missing_review_fails_the_row_not_the_batch(monkeypatch):
    with Session(get_engine("sqlite:///:memory:")) as s:
        create_coach_draft_request(s, review_id=999, requested_at=_NOW)  # dangling
        n = coach_run.process_pending(s, settings=_settings(monkeypatch, enabled=False), now=_NOW)
        assert n == 1
        req = s.query(CoachDraftRequest).one()
        assert req.status == "failed" and req.error == "review missing"
