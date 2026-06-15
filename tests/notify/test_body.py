from datetime import date

from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body


def test_daily_body_lists_picks_and_pdf_pointer():
    picks = [
        DigestPick("AMD", "medium", "Daily continuation, strong momentum."),
        DigestPick("AEP", "long", "Weekly uptrend resuming."),
    ]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert c.subject == "Swing Screener — Daily Top 5 (2026-06-15)"
    assert "1. AMD [medium] — Daily continuation, strong momentum." in c.text
    assert "AEP" in c.text and "[long]" in c.text
    assert "Full analysis attached (PDF)." in c.text
    assert "AMD" in c.html


def test_exit_alerts_section_with_badges():
    alerts = [AlertLine("MSFT", "hard", "stop", "stopped out @ 410")]
    c = compose_digest_body("daily", date(2026, 6, 15), [], alerts, has_pdf=False)
    assert "No qualifying setups" in c.text
    assert "🔴 MSFT — stop: stopped out @ 410" in c.text
    assert "Full analysis attached" not in c.text  # has_pdf=False


def test_weekly_monthly_titles():
    cw = compose_digest_body("weekly", date(2026, 6, 15), [], [], has_pdf=False)
    cm = compose_digest_body("monthly", date(2026, 6, 15), [], [], has_pdf=False)
    assert "Weekly" in cw.subject and "Monthly" in cm.subject
