"""Auditor author firewall + audit_run weekly/breach sweeps (idempotent, read-only)."""

import json
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import (
    AgentGuardrailEvent,
    AgentGuardrails,
    DisarmEvent,
    EmailLog,
    ExecutionLog,
    SystemAudit,
)
from swing_screener.db.repo import LIMIT_COUNTING_STATUSES
from swing_screener.db.session import get_engine
from swing_screener.journal import audit_run
from swing_screener.journal.audit_author import _AUDIT_SYSTEM, draft_audit
from swing_screener.journal.audit_run import _breach_scan_window, run_breach_scan, run_weekly
from swing_screener.notify import alerts
from swing_screener.settings import load_settings

_NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)
_DAY = date(2026, 7, 8)  # the single-day breach window the guardrail tests scan


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
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="cockpit"))
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
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0),
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


def test_guardrail_breach_scan_is_a_no_op_on_an_empty_book(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []
        # the auditor is READ-ONLY: peeking at the brake must not seed its row.
        assert s.query(AgentGuardrails).count() == 0


def test_weekend_disarm_is_recorded_by_mondays_scan(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(DisarmEvent(created_at=datetime(2026, 7, 11, 18, 30), reason="weekend kill"))
        s.commit()
        day_from, day_to = _breach_scan_window(date(2026, 7, 13))  # Monday's run
        rows = run_breach_scan(s, settings=s_, day_from=day_from, day_to=day_to, now=_NOW)
        assert {r.breach_key for r in rows} == {"disarm:2026-07-11"}
        # Tuesday's window overlaps the same days: idempotent, no duplicate rows.
        day_from, day_to = _breach_scan_window(date(2026, 7, 14))
        assert run_breach_scan(s, settings=s_, day_from=day_from, day_to=day_to,
                               now=_NOW) == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 1


# ---- guardrail conduct (Task 14): expected activity vs the four hard breach rules ----


def _gevent(s, *, kind, at=datetime(2026, 7, 8, 10, 0), source="digest",
            breaker="max_drawdown_usd", reason="max drawdown: $600.00 >= $500.00") -> int:
    """Append one AgentGuardrailEvent and return its id (the trip's alert_key)."""
    e = AgentGuardrailEvent(created_at=at, kind=kind, breaker=breaker, reason=reason,
                            values_json="{}", source=source)
    s.add(e)
    s.commit()
    s.refresh(e)
    return e.id


def _brake(s, *, state="ok", trip_id=None, sweep_state=None, mandate=True):
    """Seed the single agent_guardrails row in a chosen state."""
    s.add(AgentGuardrails(
        state=state, trip_id=trip_id, sweep_state=sweep_state,
        trip_reason="max drawdown: $600.00 >= $500.00" if trip_id else None,
        max_daily_loss_usd=200.0 if mandate else None,
        max_trades_per_day=3 if mandate else None,
        max_drawdown_usd=500.0 if mandate else None,
        hwm_baseline_usd=0.0, updated_at=datetime(2026, 7, 8, 9, 0)))
    s.commit()


def _live_log(*, key, run_day=_DAY, status="submitted_live"):
    """A COUNTING live execution row (what the trades/day breaker counts)."""
    return ExecutionLog(
        created_date=run_day, ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=run_day, account="live", mode="live", side="buy", limit_price=100.0,
        shares=10, stop=95.0, target=110.0, risk_dollars=50.0, notional=1000.0,
        status=status, detail="", idempotency_key=key)


def _mailed(trip_id, *, kind="guardrail"):
    return EmailLog(sent_at=datetime(2026, 7, 8, 10, 1), kind=kind,
                    subject="GUARDRAIL TRIP", run_date=_DAY, alert_key=str(trip_id))


def _keys(rows):
    return {r.breach_key for r in rows}


def test_live_counting_statuses_are_the_live_half_of_the_limit_engine():
    # The submit-while-tripped rule must count EXACTLY what the limit engine counts.
    assert set(audit_run._LIVE_COUNTING_STATUSES) <= set(LIMIT_COUNTING_STATUSES)


def test_guardrail_sweep_disarm_grades_expected_not_a_breach(monkeypatch):
    # A DisarmEvent authored by the trip protocol is the brake WORKING: counted in the
    # weekly facts, never a breach row (the trip itself already emailed the operator).
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0),
                        reason="guardrail:max_drawdown_usd", orders_cancelled=2),
            _mailed(trip_id),
        ])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_killswitch_and_halt_sweeps_grade_expected(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="kill-switch",
                        orders_cancelled=4),
            DisarmEvent(created_at=datetime(2026, 7, 8, 11, 0), reason="halt"),
        ])
        _brake(s)
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_unexplained_disarm_beside_a_sanctioned_sweep_still_breaches(monkeypatch):
    # A day mixing the brake's own sweep with an UNEXPLAINED disarm still gets its
    # breach row -- carrying only the disarm nobody sanctioned.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="halt"),
            DisarmEvent(created_at=datetime(2026, 7, 8, 12, 0), reason="mystery",
                        orders_cancelled=1),
        ])
        _brake(s)
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"disarm:2026-07-08"}
        f = json.loads(rows[0].findings_json)
        assert [d["reason"] for d in f["disarms"]] == ["mystery"]


def test_live_submit_on_a_tripped_day_warns_with_the_ordering_caveat(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([_mailed(trip_id), _live_log(key="a"),
                   _live_log(key="b", status="filled_live")])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-submit-while-tripped:2026-07-08"}
        row = rows[0]
        # day-granularity imprecision is ADMITTED, not hidden: warn, not alert.
        assert row.severity == "warn"
        assert "order" in row.narrative.lower() and "2026-07-08" in row.narrative
        assert "cannot" in row.narrative.lower()
        f = json.loads(row.findings_json)["guardrail_breach"]
        assert f["rule"] == "submit-while-tripped" and f["n_live_counting"] == 2
        assert f["caveat"]


def test_non_counting_live_rows_on_a_tripped_day_are_not_a_submit_breach(monkeypatch):
    # A 'guardrail: ...' clamp and a rejection never reserved anything -- they are the
    # brake working, and counting them would flag exactly the correct days.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([_mailed(trip_id), _live_log(key="a", status="skipped"),
                   _live_log(key="b", status="rejected_live")])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_live_submit_after_the_trip_cleared_is_not_a_submit_breach(monkeypatch):
    # State at the END of the day is what the rule reads: a clear that landed the same
    # day means the book was released -- submitting again is sanctioned.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        _gevent(s, kind="clear", at=datetime(2026, 7, 8, 11, 0), source="cockpit",
                breaker="", reason=f"trip {trip_id} acknowledged and cleared")
        s.add_all([_mailed(trip_id), _live_log(key="a")])
        _brake(s)
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_trip_with_no_alert_email_is_a_breach(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip", source="screen")
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-unmailed-trip:2026-07-08"}
        assert rows[0].severity == "alert"
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["trip_event_ids"] == [trip_id] and f["rule"] == "unmailed-trip"


def test_mailed_trip_is_not_a_breach(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add(_mailed(trip_id))
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_execution_cover_bookkeeping_never_counts_as_the_trip_alert(monkeypatch):
    # kind hygiene (Task 11): 'execution-cover' rows are per-row coverage markers, not
    # sent emails -- one must never satisfy the trip-alert contract.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add(_mailed(trip_id, kind="execution-cover"))
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-unmailed-trip:2026-07-08"}


def test_cleared_trip_is_exempt_from_the_unmailed_breach(monkeypatch):
    # Task 10's emitter deliberately never mails a trip the operator already CLEARED
    # (clearing implies awareness); the auditor must exempt it for the same reason.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        _gevent(s, kind="clear", at=datetime(2026, 7, 8, 11, 0), source="cockpit",
                breaker="", reason=f"trip {trip_id} acknowledged and cleared")
        _brake(s)
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_stuck_partial_sweep_older_than_a_day_is_a_breach(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add(_mailed(trip_id))
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="partial")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-stuck-sweep:2026-07-08"}
        assert rows[0].severity == "alert"
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["sweep_state"] == "partial" and f["trip_id"] == trip_id


def test_todays_pending_sweep_is_not_yet_stuck(monkeypatch):
    # The resume owner runs every cycle; a sweep is only "stuck" once it has survived
    # a full day of retries.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip", at=datetime(2026, 7, 12, 9, 0))
        s.add(_mailed(trip_id))
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="pending")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=date(2026, 7, 12),
                               now=_NOW) == []


def test_completed_sweep_is_never_stuck(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add(_mailed(trip_id))
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_live_submissions_with_no_mandatory_breakers_set_warns(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_live_log(key="a"))
        _brake(s, mandate=False)
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-unset-mandate:2026-07-08"}
        assert rows[0].severity == "warn"
        # it cannot know the host: a paper-endpoint drill reads identically.
        assert "paper" in rows[0].narrative.lower()


def test_live_submissions_under_a_full_mandate_are_clean(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_live_log(key="a"))
        _brake(s, mandate=True)
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_guardrail_breach_rows_are_idempotent(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([_live_log(key="a"), _live_log(key="b", status="filled_live")])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="partial", mandate=False)
        first = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(first) == {
            "guardrail-submit-while-tripped:2026-07-08",
            "guardrail-unmailed-trip:2026-07-08",
            "guardrail-stuck-sweep:2026-07-08",
            "guardrail-unset-mandate:2026-07-08",
        }
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 4


def test_weekly_grades_a_sanctioned_brake_week_as_expected_conduct(monkeypatch):
    # The brake firing correctly is INFO-grade conduct: counted in the facts, never
    # escalated. Only a disarm nobody sanctioned pushes the week to 'warn'.
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0),
                        reason="guardrail:max_drawdown_usd", orders_cancelled=2),
            DisarmEvent(created_at=datetime(2026, 7, 8, 11, 0), reason="halt"),
            _mailed(trip_id),
        ])
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW)
        assert a.severity == "info"
        comp = json.loads(a.findings_json)["compliance"]
        assert comp["n_guardrail_sweeps"] == 1 and comp["n_halt_sweeps"] == 1
        assert comp["n_guardrail_trips"] == 1 and comp["n_unexplained_disarms"] == 0
        assert "1 guardrail trip" in a.narrative


def test_weekly_still_warns_on_an_unexplained_disarm(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="mystery"))
        s.commit()
        a = run_weekly(s, settings=s_, period_from=_FROM, period_to=_TO, now=_NOW)
        assert a.severity == "warn"


# ---- review round: the two clocks (rule 1) and trip EPISODES (rule 2) ----


def test_friday_run_date_orders_meet_mondays_trip(monkeypatch):
    """THE PRODUCTION LAYOUT, and why the rule bridges two clocks: the evening screen
    stamps ``run_date=Friday``, the digest DISPATCHES those orders Monday morning, and
    Monday's consult trips the brake. Matching run_date to the trip's wall-clock day
    would compare Friday to Monday, see nothing, and leave the rule structurally inert
    on the only path that submits live orders."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        friday, monday = date(2026, 7, 10), date(2026, 7, 13)
        trip_id = _gevent(s, kind="trip", at=datetime(2026, 7, 13, 9, 35))
        s.add_all([_mailed(trip_id), _live_log(key="a", run_day=friday),
                   _live_log(key="b", run_day=friday, status="filled_live")])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=friday, day_to=monday,
                               now=datetime(2026, 7, 13, 20, 0, tzinfo=UTC))
        assert _keys(rows) == {"guardrail-submit-while-tripped:2026-07-10"}
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["n_live_counting"] == 2 and f["trip_day"] == "2026-07-13"
        assert rows[0].severity == "warn" and f["caveat"]


def test_trip_cleared_before_the_dispatch_day_does_not_fire(monkeypatch):
    """The inverse: Friday's trip was acknowledged and cleared before Monday's dispatch
    of the Friday-stamped orders, so submitting them was sanctioned."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        friday, monday = date(2026, 7, 10), date(2026, 7, 13)
        trip_id = _gevent(s, kind="trip", at=datetime(2026, 7, 10, 16, 5))
        _gevent(s, kind="clear", at=datetime(2026, 7, 10, 17, 0), source="cockpit",
                breaker="", reason=f"trip {trip_id} acknowledged and cleared")
        s.add_all([_mailed(trip_id), _live_log(key="a", run_day=friday)])
        _brake(s)
        assert run_breach_scan(s, settings=s_, day_from=friday, day_to=monday,
                               now=datetime(2026, 7, 13, 20, 0, tzinfo=UTC)) == []


def test_election_loser_trip_event_rides_the_winners_alert(monkeypatch):
    """``guardrails_repo.trip()`` appends the trip event UNCONDITIONALLY, BEFORE the
    rows-affected election: on a concurrent breach the loser's event is permanently
    unmailed by design (``respond_to_trip`` touches nothing on a lost election, and the
    alert is keyed on the WINNER's id). One episode, one required email."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        winner = _gevent(s, kind="trip", source="digest")
        _gevent(s, kind="trip", source="exitcheck", at=datetime(2026, 7, 8, 10, 0, 1))
        s.add(_mailed(winner))
        _brake(s, state="tripped", trip_id=winner, sweep_state="complete")
        assert run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW) == []


def test_wholly_unmailed_episode_still_breaches_once(monkeypatch):
    """A genuinely unmailed episode -- neither the winner nor the loser mailed -- is
    still ONE breach, carrying every trip id in the episode."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        winner = _gevent(s, kind="trip", source="digest")
        loser = _gevent(s, kind="trip", source="exitcheck",
                        at=datetime(2026, 7, 8, 10, 0, 1))
        _brake(s, state="tripped", trip_id=winner, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        assert _keys(rows) == {"guardrail-unmailed-trip:2026-07-08"}
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["trip_event_ids"] == [winner, loser] and f["n_episodes"] == 1
        assert "episode" in rows[0].narrative.lower()


def test_second_episode_after_a_clear_breaches_alone(monkeypatch):
    """A 'clear' ENDS an episode: the mailed first episode covers only its own trips,
    and the unmailed episode that follows breaches on its own day."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        first = _gevent(s, kind="trip")
        s.add(_mailed(first))
        _gevent(s, kind="clear", at=datetime(2026, 7, 8, 11, 0), source="cockpit",
                breaker="", reason=f"trip {first} acknowledged and cleared")
        second = _gevent(s, kind="trip", at=datetime(2026, 7, 9, 10, 0), source="screen")
        _brake(s, state="tripped", trip_id=second, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=date(2026, 7, 9),
                               now=_NOW)
        assert _keys(rows) == {"guardrail-unmailed-trip:2026-07-09"}
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["trip_event_ids"] == [second]


def test_trip_alert_kind_matches_the_module_that_writes_it():
    """Anti-drift pin: the auditor restates the trip-alert EmailLog kind as a literal
    (no journal -> notify import); ``notify.alerts`` OWNS it."""
    assert audit_run._TRIP_ALERT_KIND == alerts.TRIP_ALERT_KIND


# ---- review round 3: window-stable keys + what the bridge can honestly claim ----


def test_unmailed_episode_spanning_the_window_edge_keys_on_its_first_trip(monkeypatch):
    """The breach key must be a property of the EPISODE, not of the sliding window.
    An open episode whose trips straddle two scans (07-08 and 07-10) would otherwise
    emit one row per window -- same trip ids, different day keys, and the daily re-scan
    makes that a recurring duplicate rather than a one-off."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        first = _gevent(s, kind="trip", source="digest")
        later = _gevent(s, kind="trip", at=datetime(2026, 7, 10, 9, 30), source="screen")
        _brake(s, state="tripped", trip_id=first, sweep_state="complete")
        early = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 6),
                                day_to=date(2026, 7, 12), now=_NOW)
        assert _keys(early) == {"guardrail-unmailed-trip:2026-07-08"}
        assert json.loads(early[0].findings_json)["guardrail_breach"][
            "trip_event_ids"] == [first, later]
        # the window slides past the episode's first trip: SAME episode, same key.
        late = run_breach_scan(s, settings=s_, day_from=date(2026, 7, 9),
                               day_to=date(2026, 7, 15),
                               now=datetime(2026, 7, 15, 20, 0, tzinfo=UTC))
        assert late == []
        assert s.query(SystemAudit).filter_by(kind="breach").count() == 1


def test_submit_breach_records_a_clear_that_landed_after_the_bridge_window(monkeypatch):
    """The bridge window can only see events through R+4, so it must not claim a trip
    was NEVER cleared -- only that no clear landed inside the window it read. When the
    operator cleared later, the permanent row says so."""
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")                      # 07-08, bridge -> 07-12
        s.add_all([_mailed(trip_id), _live_log(key="a")])
        _gevent(s, kind="clear", at=datetime(2026, 7, 15, 9, 0), source="cockpit",
                breaker="", reason=f"trip {trip_id} acknowledged and cleared")
        _brake(s)
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=date(2026, 7, 16),
                               now=datetime(2026, 7, 16, 20, 0, tzinfo=UTC))
        assert _keys(rows) == {"guardrail-submit-while-tripped:2026-07-08"}
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["cleared_at"].startswith("2026-07-15")
        assert "never cleared" not in f["detail"]          # a claim it cannot make
        assert "bridge window" in f["detail"]
        assert "later cleared 2026-07-15" in rows[0].narrative


def test_genuinely_uncleared_trip_records_no_clear(monkeypatch):
    s_ = _settings(monkeypatch, audit_enabled=False)
    with Session(get_engine("sqlite:///:memory:")) as s:
        trip_id = _gevent(s, kind="trip")
        s.add_all([_mailed(trip_id), _live_log(key="a")])
        _brake(s, state="tripped", trip_id=trip_id, sweep_state="complete")
        rows = run_breach_scan(s, settings=s_, day_from=_DAY, day_to=_DAY, now=_NOW)
        f = json.loads(rows[0].findings_json)["guardrail_breach"]
        assert f["cleared_at"] is None
        assert "later cleared" not in rows[0].narrative
