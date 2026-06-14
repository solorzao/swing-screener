from datetime import date

from swing_screener.db.models import ExitEvent
from swing_screener.notify.alerts import compose_exit_alert

RUN = date(2026, 6, 15)


def _ev(tier, reason, message):
    return ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier=tier,
                     reason=reason, message=message)


def test_hard_stop_makes_subject_urgent():
    events = [_ev("hard", "stop", "AMD stopped @ 95"),
              _ev("advisory", "target", "AEP hit target @ 110")]
    c = compose_exit_alert(events, RUN)
    assert "URGENT" in c.subject
    assert "🔴 stop: AMD stopped @ 95" in c.text
    assert "🟡 target: AEP hit target @ 110" in c.text
    assert "AMD" in c.html


def test_no_hard_stop_is_not_urgent():
    c = compose_exit_alert([_ev("advisory", "time_stop", "MSFT timed out")], RUN)
    assert "URGENT" not in c.subject and "Exit Alerts" in c.subject
