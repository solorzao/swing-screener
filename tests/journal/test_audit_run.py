"""Auditor author firewall + audit_run weekly/breach sweeps (idempotent, read-only)."""

import json
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import DisarmEvent, ExecutionLog, SystemAudit
from swing_screener.db.session import get_engine
from swing_screener.journal import audit_run
from swing_screener.journal.audit_author import _AUDIT_SYSTEM, draft_audit
from swing_screener.journal.audit_run import _breach_scan_window, run_breach_scan, run_weekly
from swing_screener.settings import load_settings

_NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)


class _Block:
    type = "text"
    def __init__(self, t): self.text = t


class _Usage:
    input_tokens = 100
    output_tokens = 50


class _Resp:
    def __init__(self, t, usage=None):
        self.content = [_Block(t)]
        self.usage = usage


class _FakeClient:
    def __init__(self, text=None, raises=False, usage=None):
        self._t, self._raises, self._usage = text, raises, usage
        self.last_kwargs: dict = {}
        self.messages = self
    def create(self, **kw):
        self.last_kwargs = kw
        if self._raises:
            raise RuntimeError("down")
        return _Resp(self._t, usage=self._usage)


def _settings(monkeypatch, *, audit_enabled=False, max_notional=None):
    for k in ("SWING_AUDIT_ENABLED", "SWING_AUDIT_MAX_USD", "SWING_MAX_DAILY_NOTIONAL"):
        monkeypatch.delenv(k, raising=False)
    if audit_enabled:
        monkeypatch.setenv("SWING_AUDIT_ENABLED", "1")
    if max_notional is not None:
        monkeypatch.setenv("SWING_MAX_DAILY_NOTIONAL", str(max_notional))
    return load_settings()


def _log(*, notional, status="filled_paper", key):
    return ExecutionLog(
        created_date=date(2026, 7, 8), ticker="AMD", timeframe="1d",
        play_type="continuation", run_date=date(2026, 7, 8), account="paper", mode="paper",
        side="buy", limit_price=100.0, shares=10, stop=95.0, target=110.0,
        risk_dollars=50.0, notional=notional, status=status, detail="", idempotency_key=key)


# ---- author firewall ----

def test_draft_audit_returns_client_prose():
    out = draft_audit({"compliance": {}, "anomaly": {}}, client=_FakeClient(text="Clean week."),
                      model="claude-haiku-4-5")
    assert out.text == "Clean week."


def test_draft_audit_degrades_to_template():
    out = draft_audit({"compliance": {"cap_breaches": []}, "anomaly": {}},
                      client=_FakeClient(raises=True), model="claude-haiku-4-5")
    assert out.usage is None and "conduct audit" in out.text.lower()


def test_audit_system_prompt_is_independent_voice():
    assert "INDEPENDENT" in _AUDIT_SYSTEM and "MUST NOT" in _AUDIT_SYSTEM


# ---- weekly sweep ----

def test_run_weekly_writes_one_row_and_is_idempotent(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        a1 = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW)
        assert a1.kind == "weekly" and a1.narrative is not None
        a2 = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW)
        assert s.query(SystemAudit).filter_by(kind="weekly").count() == 1  # upsert, not dup
        assert a2.id == a1.id


def test_enabled_but_clean_week_skips_the_llm(monkeypatch):
    # $0 on a dead week: enabled + nothing to report -> deterministic template, no LLM.
    s_ = _settings(monkeypatch, audit_enabled=True)
    with Session(get_engine("sqlite:///:memory:")) as s:
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW,
                       client=_FakeClient(text="LLM-NARRATION"))
        assert "LLM-NARRATION" not in (a.narrative or "")   # LLM was NOT called
        assert "conduct audit" in a.narrative.lower()        # template instead
        assert a.est_cost_usd is None and a.severity == "info"


def test_enabled_with_a_finding_calls_the_llm(monkeypatch):
    # something to report (a cap breach) -> the LLM narrates.
    s_ = _settings(monkeypatch, audit_enabled=True, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])  # 1600 > 1000
        s.commit()
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW,
                       client=_FakeClient(text="LLM-NARRATION"))
        assert a.narrative == "LLM-NARRATION" and a.severity == "alert"


def test_stamped_model_matches_the_model_actually_called(monkeypatch):
    # Provenance unification (2026-07-17 audit): SystemAudit.model must equal the model
    # id the API call was actually made with -- both come from audit_run._MODEL, the
    # worker's ONE authoritative constant, so spend can never be misattributed.
    s_ = _settings(monkeypatch, audit_enabled=True, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])
        s.commit()
        client = _FakeClient(text="LLM-NARRATION", usage=_Usage())
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW,
                       client=client)
        assert a.model == audit_run._MODEL == "claude-haiku-4-5"
        assert client.last_kwargs["model"] == audit_run._MODEL
        assert a.est_cost_usd is not None and a.est_cost_usd > 0


def test_run_weekly_severity_alert_on_cap_breach(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])  # 1600 > 1000
        s.commit()
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW)
        assert a.severity == "alert"
        assert json.loads(a.findings_json)["compliance"]["cap_breaches"]


# ---- breach scan ----

def test_breach_scan_writes_cap_and_disarm_breaches_idempotently(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC), reason="cockpit"))
        s.commit()
        rows = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 8),
                               day_to=date(2026, 7, 8), now=_NOW)
        keys = {r.breach_key for r in rows}
        assert keys == {"cap:2026-07-08:paper", "disarm:2026-07-08"}
        # re-run: nothing new (idempotent)
        again = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 8),
                                day_to=date(2026, 7, 8), now=_NOW)
        assert again == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 2


def test_breach_row_narrates_the_actual_breach(monkeypatch):
    """An ALERT breach row must state what fired -- not render the period template over
    empty findings ("0 cap breach(es); ...; 0 disarm(s)" on a breach row is a lie)."""
    s_ = _settings(monkeypatch, audit_enabled=False, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
                          reason="cockpit kill switch", orders_cancelled=3))
        s.commit()
        rows = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 8),
                               day_to=date(2026, 7, 8), now=_NOW)
        by_key = {r.breach_key: r for r in rows}
        cap = by_key["cap:2026-07-08:paper"].narrative
        assert "0 cap breach" not in cap  # the old empty-findings render
        assert "paper" in cap and "2026-07-08" in cap
        assert "1600" in cap and "1000" in cap  # the actual values vs the cap
        dis = by_key["disarm:2026-07-08"].narrative
        assert "0 cap breach" not in dis and "0 disarm" not in dis
        assert "cockpit kill switch" in dis and "3 order(s) cancelled" in dis
        # findings_json is authoritative: the disarm row carries the actual events.
        f = json.loads(by_key["disarm:2026-07-08"].findings_json)
        assert f["disarms"][0]["reason"] == "cockpit kill switch"
        assert f["disarms"][0]["orders_cancelled"] == 3


def test_breach_scan_window_covers_the_trailing_week():
    # The scan runs weekdays 16:00 ET: Monday's window must reach back over the whole
    # weekend AND yesterday's post-scan tail (a today-only window records neither).
    day_from, day_to = _breach_scan_window(date(2026, 7, 13))  # a Monday
    assert day_to == date(2026, 7, 13)
    assert day_from <= date(2026, 7, 11)  # Saturday is in-window


def test_breach_recorded_after_yesterdays_scan_is_caught_today(monkeypatch):
    # A cap breach dated 2026-07-08 that landed AFTER that day's 16:00 scan: the next
    # run's window still covers 07-08, so it is recorded a day late instead of never.
    s_ = _settings(monkeypatch, audit_enabled=False, max_notional=1000.0)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, key="a"), _log(notional=800.0, key="b")])
        s.commit()
        day_from, day_to = _breach_scan_window(date(2026, 7, 9))
        rows = run_breach_scan(s, settings=s_, day_from=day_from, day_to=day_to, now=_NOW)
        assert {r.breach_key for r in rows} == {"cap:2026-07-08:paper"}


def test_weekend_disarm_is_recorded_by_mondays_scan(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(DisarmEvent(created_at=datetime(2026, 7, 11, 18, 30, tzinfo=UTC), reason="weekend kill"))
        s.commit()
        day_from, day_to = _breach_scan_window(date(2026, 7, 13))  # Monday's run
        rows = run_breach_scan(s, settings=s_, day_from=day_from, day_to=day_to, now=_NOW)
        assert {r.breach_key for r in rows} == {"disarm:2026-07-11"}
        # Tuesday's window overlaps the same days: idempotent, no duplicate rows.
        day_from, day_to = _breach_scan_window(date(2026, 7, 14))
        assert run_breach_scan(s, settings=s_, day_from=day_from, day_to=day_to,
                               now=_NOW) == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 1
