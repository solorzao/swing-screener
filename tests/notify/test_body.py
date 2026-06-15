from datetime import date

from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body


def test_daily_body_lists_picks_and_pdf_pointer():
    picks = [
        DigestPick("AMD", "Advanced Micro Devices", "medium",
                   "Daily continuation, strong momentum."),
        DigestPick("AEP", "American Electric Power", "long",
                   "Weekly uptrend resuming."),
    ]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert c.subject == "Swing Screener — Daily Top 5 (2026-06-15)"
    assert ("1. AMD - Advanced Micro Devices [medium] — "
            "Daily continuation, strong momentum.") in c.text
    assert "AEP" in c.text and "American Electric Power" in c.text and "[long]" in c.text
    assert "Full analysis attached (PDF)." in c.text
    assert "AMD" in c.html and "Advanced Micro Devices" in c.html


def test_deep_picks_are_labelled_in_body():
    picks = [
        DigestPick("AMD", "Advanced Micro Devices", "medium", "Deep read.", is_deep=True),
        DigestPick("AEP", "American Electric Power", "long", "Standard."),
    ]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert "[medium · Deep Analysis]" in c.text   # deep pick flagged
    assert "[long] —" in c.text                   # standard pick unchanged
    assert "Deep Analysis</b>" in c.html


def test_empty_company_name_omits_dash():
    picks = [DigestPick("AMD", "", "medium", "Daily continuation.")]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "1. AMD [medium] — Daily continuation." in c.text
    assert "AMD -  [" not in c.text  # no dangling "- " with empty name
    assert "AMD [medium]" in c.html


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
