"""Auditor author firewall + audit_run weekly/breach sweeps (idempotent, read-only)."""

import json
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import DisarmEvent, ExecutionLog, SystemAudit
from swing_screener.db.session import get_engine
from swing_screener.journal.audit_author import _AUDIT_SYSTEM, draft_audit
from swing_screener.journal.audit_run import run_breach_scan, run_weekly
from swing_screener.settings import load_settings

_NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)


class _Block:
    type = "text"
    def __init__(self, t): self.text = t


class _Resp:
    def __init__(self, t):
        self.content = [_Block(t)]
        self.usage = None


class _FakeClient:
    def __init__(self, text=None, raises=False):
        self._t, self._raises = text, raises
        self.messages = self
    def create(self, **kw):
        if self._raises:
            raise RuntimeError("down")
        return _Resp(self._t)


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
    out = draft_audit({"compliance": {}, "anomaly": {}}, client=_FakeClient(text="Clean week."))
    assert out.text == "Clean week."


def test_draft_audit_degrades_to_template():
    out = draft_audit({"compliance": {"cap_breaches": []}, "anomaly": {}},
                      client=_FakeClient(raises=True))
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
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="cockpit"))
        s.commit()
        rows = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 8),
                               day_to=date(2026, 7, 8), now=_NOW)
        keys = {r.breach_key for r in rows}
        assert keys == {"cap:2026-07-08", "disarm:2026-07-08"}
        # re-run: nothing new (idempotent)
        again = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 8),
                                day_to=date(2026, 7, 8), now=_NOW)
        assert again == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 2
