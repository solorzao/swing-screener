"""Auditor compliance grader: cap adherence, reject/clamp rate, disarm count -- all
over MACHINE conduct data only (ExecutionLog, DisarmEvent). Never touches Trade."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import DisarmEvent, ExecutionLog
from swing_screener.db.session import get_engine
from swing_screener.journal.audit_compliance import compliance_findings

_FROM, _TO = date(2026, 7, 6), date(2026, 7, 12)


def _log(*, created=date(2026, 7, 7), notional=1000.0, risk=100.0, status="filled_paper"):
    return ExecutionLog(
        created_date=created, ticker="AMD", timeframe="1d", play_type="continuation",
        run_date=created, account="paper", mode="paper", side="buy", limit_price=100.0,
        shares=10, stop=95.0, target=110.0, risk_dollars=risk, notional=notional,
        status=status, detail="", idempotency_key=f"k-{created}-{notional}-{status}",
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
