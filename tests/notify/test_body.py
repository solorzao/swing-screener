from datetime import date

from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body


def test_daily_body_lists_picks_and_pdf_pointer():
    picks = [
        DigestPick("AMD", "Advanced Micro Devices", "medium",
                   "Daily continuation, strong momentum.", score=0.92),
        DigestPick("AEP", "American Electric Power", "long",
                   "Weekly uptrend resuming.", score=0.85),
    ]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert c.subject == "Swing Screener - Daily Picks (2026-06-15)"  # no "Top 5", hyphen
    assert "Top 5 - Continuation Plays" in c.text       # section title, not the repeated subject
    assert c.subject not in c.text                       # subject is NOT echoed in the body
    assert ("1. AMD - Advanced Micro Devices [medium] · score 0.92 — "
            "Daily continuation, strong momentum.") in c.text
    assert "AEP" in c.text and "American Electric Power" in c.text and "[long]" in c.text
    assert "Full analysis attached (PDF)." in c.text
    assert "<h3>Top 5 - Continuation Plays</h3>" in c.html
    assert "score <b>0.92</b>" in c.html                 # scores bold in HTML
    assert "<b>AMD</b>" in c.html and "Advanced Micro Devices" in c.html


def test_deep_picks_are_labelled_in_body():
    picks = [
        DigestPick("AMD", "Advanced Micro Devices", "medium", "Deep read.", score=0.9,
                   is_deep=True),
        DigestPick("AEP", "American Electric Power", "long", "Standard.", score=0.8),
    ]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert "[medium · Deep Analysis]" in c.text    # deep pick flagged
    assert "[long] · score" in c.text              # standard pick unchanged (no deep tag)
    assert "Deep Analysis</b>" in c.html


def test_empty_company_name_omits_dash():
    picks = [DigestPick("AMD", "", "medium", "Daily continuation.", score=0.77)]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "1. AMD [medium] · score 0.77 — Daily continuation." in c.text
    assert "AMD -  [" not in c.text  # no dangling "- " with empty name
    assert "<b>AMD</b> · [medium]" in c.html


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
