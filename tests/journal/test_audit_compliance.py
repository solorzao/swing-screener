"""Auditor compliance grader: cap adherence, reject/clamp rate, disarm count -- all
over MACHINE conduct data only (ExecutionLog, DisarmEvent). Never touches Trade."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import (
    AgentGuardrailEvent,
    DisarmEvent,
    EmailLog,
    ExecutionLog,
    PaperTrade,
)
from swing_screener.db.session import get_engine
from swing_screener.journal import audit_compliance
from swing_screener.journal.audit_compliance import compliance_findings
from swing_screener.notify import alerts

_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)


def _log(*, created=date(2026, 7, 7), notional=1000.0, risk=100.0, status="filled_paper",
         account="paper", detail=""):
    return ExecutionLog(
        created_date=created, ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=created, account=account, mode="paper", side="buy", limit_price=100.0,
        shares=10, stop=95.0, target=110.0, risk_dollars=risk, notional=notional,
        status=status, detail=detail,
        idempotency_key=f"k-{created}-{notional}-{status}-{account}",
    )


def test_cap_breach_flagged_when_daily_notional_exceeds_mandate():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=6000.0), _log(notional=6500.0)])  # distinct keys
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=10_000.0, max_daily_loss=None)
        assert len(f.cap_breaches) == 1                # 12500 > 10000 on 2026-07-07
        assert f.cap_breaches[0]["notional"] == 12500.0


def test_no_breach_when_under_caps_or_caps_none():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_log(notional=1000.0))
        s.commit()
        assert compliance_findings(s, period_from=_FROM, period_to=_TO,
                                   max_daily_notional=None, max_daily_loss=None).cap_breaches == []


def test_clamped_day_is_not_a_breach():
    # A "skipped" clamp row carries the BLOCKED order's full size; the limit engine
    # never counted it, so neither may the auditor. 900 filled <= 1000 cap -> clean --
    # this is the machine behaving CORRECTLY (filled to the cap, then clamped).
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=900.0, status="filled_paper"),
                   _log(notional=500.0, status="skipped")])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=1000.0, max_daily_loss=None)
        assert f.cap_breaches == []
        assert f.n_total == 2          # reject_rate still sees ALL rows
        assert f.n_clamps == 1         # the clamp is surfaced, not flagged


def test_caps_grade_per_account():
    # Caps are enforced PER account (execution_logs_for_day filters on account); pooling
    # 800 paper + 800 live into 1600 would false-flag a 1000 cap neither account hit.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(notional=800.0, account="paper", status="filled_paper"),
                   _log(notional=800.0, account="live", status="filled_live")])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=1000.0, max_daily_loss=None)
        assert f.cap_breaches == []


def test_real_breach_still_fires():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_log(notional=1100.0))
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=1000.0, max_daily_loss=None)
        assert len(f.cap_breaches) == 1
        assert f.cap_breaches[0]["account"] == "paper"


def _closed(*, exit_day=date(2026, 7, 7), realized_r=-1.0, account="paper"):
    """A closed shadow-book trade -- what realized_r_on (the breaker's source) sums."""
    return PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", account=account,
                      signal_score=0.5, rank=1, fill_status="filled", status="closed",
                      stop=95.0, target=110.0, risk=5.0, realized_r=realized_r,
                      exit_date=exit_day)


def test_realized_minus_3R_day_breaches_with_2R_cap():
    # max_daily_loss is the execution breaker's R threshold on REALIZED loss
    # (execution.py PER-DAY-LOSS UNIT DECISION): -3R realized vs a 2R cap breaches.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(), _closed(realized_r=-3.0)])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=2.0)
        assert len(f.cap_breaches) == 1
        b = f.cap_breaches[0]
        assert b["day_r"] == -3.0 and b["loss_cap_r"] == 2.0 and b["account"] == "paper"


def test_entry_risk_alone_never_breaches_loss_cap():
    # $500 committed at entry with nothing realized is NOT a loss; grading entry risk
    # against an R-tuned cap (2.0) would flag essentially every executed day.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add(_log(risk=500.0))
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=2.0)
        assert f.cap_breaches == []


def test_positive_day_never_breaches():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_log(), _closed(realized_r=1.5)])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=2.0)
        assert f.cap_breaches == []


def test_reject_and_clamp_rate():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            _log(status="filled_paper"), _log(status="rejected", notional=1.0),
            _log(status="skipped", notional=2.0), _log(status="rejected_live", notional=3.0),
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_total == 4 and f.n_rejected == 3
        assert f.reject_rate == 0.75


def test_disarm_count_in_period_only():
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0), reason="cockpit"),
            DisarmEvent(created_at=datetime(2026, 6, 1, 10, 0), reason="old"),  # out of period
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_disarms == 1


def test_empty_period_is_all_zero_not_a_crash():
    with Session(get_engine("sqlite:///:memory:")) as s:
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=10_000.0, max_daily_loss=500.0)
        assert f.cap_breaches == [] and f.reject_rate is None and f.n_disarms == 0
        assert f.n_guardrail_clamps == 0 and f.n_guardrail_sweeps == 0
        assert f.n_guardrail_trips == 0 and f.trip_sources == {}


# ---- guardrail conduct: EXPECTED activity, counted as facts (the n_clamps precedent) ----


def _event(*, kind, at=datetime(2026, 7, 8, 10, 0), source="digest", breaker="",
           reason=""):
    return AgentGuardrailEvent(created_at=at, kind=kind, breaker=breaker, reason=reason,
                               values_json="{}", source=source)


def test_guardrail_clamp_is_counted_as_expected_conduct_not_a_breach():
    # The submit-side brake clamps a would-be order to a 'skipped' row whose detail
    # starts 'guardrail: ' -- the brake DOING ITS JOB. Same posture as n_clamps:
    # surfaced as a fact, never a cap breach (the blocked size never counted).
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            _log(notional=900.0, status="filled_paper"),
            _log(notional=5000.0, status="skipped",
                 detail="guardrail: max drawdown: $600.00 >= $500.00"),
            _log(notional=4000.0, status="skipped", risk=1.0,
                 detail="daily notional cap reached"),   # a plain limit clamp
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=1000.0, max_daily_loss=None)
        assert f.cap_breaches == []          # clamped size never counted
        assert f.n_clamps == 2               # both clamps still surfaced
        assert f.n_guardrail_clamps == 1     # ...one of them the brake's


def test_guardrail_killswitch_and_halt_sweeps_grade_expected():
    # Every sanctioned venue-moving sweep is counted by NAME; only a disarm the
    # guardrails machinery did NOT author is 'unexplained' (what the breach scan flags).
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0),
                        reason="guardrail:max_drawdown_usd", orders_cancelled=2),
            DisarmEvent(created_at=datetime(2026, 7, 8, 11, 0), reason="kill-switch"),
            DisarmEvent(created_at=datetime(2026, 7, 8, 12, 0), reason="halt"),
            DisarmEvent(created_at=datetime(2026, 7, 9, 9, 0), reason="cockpit"),
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_disarms == 4              # the total is unchanged
        assert f.n_guardrail_sweeps == 1 and f.n_killswitch_sweeps == 1
        assert f.n_halt_sweeps == 1
        assert f.n_unexplained_disarms == 1  # only the bare 'cockpit' disarm


def test_two_sweeps_in_one_hour_is_a_resumed_sweep_not_an_anomaly():
    # Task 11 removed the double-sweep; a second DisarmEvent an hour later is a
    # RESUMED partial sweep. Counted, never graded as anomalous frequency.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 0),
                        reason="guardrail:max_daily_loss_usd"),
            DisarmEvent(created_at=datetime(2026, 7, 8, 10, 59),
                        reason="guardrail:max_daily_loss_usd"),
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_guardrail_sweeps == 2 and f.n_unexplained_disarms == 0


def test_trips_from_all_four_emitters_are_legitimate_facts():
    # digest / screen / exitcheck / cockpit all legitimately own a trip election.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([_event(kind="trip", source=src, breaker="max_drawdown_usd")
                   for src in ("digest", "screen", "exitcheck", "cockpit")])
        s.add(_event(kind="edit", source="cockpit"))       # not a trip
        s.add(_event(kind="trip", source="digest", at=datetime(2026, 6, 1, 10, 0)))
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_guardrail_trips == 4      # the out-of-period trip is excluded
        assert f.trip_sources == {"cockpit": 1, "digest": 1, "exitcheck": 1, "screen": 1}


def test_email_counts_exclude_execution_cover_bookkeeping():
    # 'execution-cover' rows are per-ExecutionLog coverage markers, NOT sent emails
    # (Task 11): counting them would inflate every "emails sent" conduct number.
    with Session(get_engine("sqlite:///:memory:")) as s:
        s.add_all([
            EmailLog(sent_at=datetime(2026, 7, 8, 10, 0), kind="guardrail",
                     subject="TRIP", run_date=date(2026, 7, 8), alert_key="7"),
            EmailLog(sent_at=datetime(2026, 7, 8, 10, 1), kind="execution",
                     subject="3 Live Orders Rejected", run_date=date(2026, 7, 8),
                     alert_key="sethash"),
            EmailLog(sent_at=datetime(2026, 7, 8, 10, 1), kind="execution-cover",
                     subject="3 Live Orders Rejected", run_date=date(2026, 7, 8),
                     alert_key="xlog-11"),
            EmailLog(sent_at=datetime(2026, 7, 8, 10, 1), kind="execution-cover",
                     subject="3 Live Orders Rejected", run_date=date(2026, 7, 8),
                     alert_key="xlog-12"),
        ])
        s.commit()
        f = compliance_findings(s, period_from=_FROM, period_to=_TO,
                                max_daily_notional=None, max_daily_loss=None)
        assert f.n_emails_sent == 2          # the two real emails, not the 2 markers
        assert f.n_guardrail_alerts == 1


def test_email_kind_mirrors_match_the_module_that_writes_them():
    # The grader restates both kinds as literals (no journal -> notify import in the
    # pure grader); these are the anti-drift pins against the module that WRITES them.
    assert audit_compliance._BOOKKEEPING_EMAIL_KIND == alerts.REJECTION_COVER_KIND
    assert audit_compliance._TRIP_ALERT_KIND == alerts.TRIP_ALERT_KIND
