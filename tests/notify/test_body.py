from datetime import date, datetime

from swing_screener.notify.body import AlertLine, DigestPick, compose_digest_body
from swing_screener.notify.ticker_report import TickerReport, TimeframeRead


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


def test_reversal_section_renders_with_strength_tag():
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    rev = [DigestPick("GME", "GameStop", "short", "Oversold bounce.", score=0.7,
                      strength="confirmed")]
    c = compose_digest_body("daily", date(2026, 6, 15), cont, [], has_pdf=False,
                            reversal_picks=rev)
    assert "Top 5 - Continuation Plays" in c.text and "Top 5 - Reversal Plays" in c.text
    assert "[short · confirmed]" in c.text          # reversal strength tagged
    assert "<h3>Top 5 - Reversal Plays</h3>" in c.html


def test_reversal_section_omitted_when_none():
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    c = compose_digest_body("weekly", date(2026, 6, 15), cont, [], has_pdf=False)  # no reversals
    assert "Reversal Plays" not in c.text and "Reversal Plays" not in c.html


def _report():
    reads = [
        TimeframeRead(timeframe="1d", ha_trend="bullish", ema_aligned=True,
                      rsi=58.0, atr_pct=0.04, setup=None, chart_path=None),
        TimeframeRead(timeframe="1wk", ha_trend="bearish", ema_aligned=False,
                      rsi=44.0, atr_pct=0.06, setup=None, chart_path=None),
    ]
    return TickerReport(
        ticker="AMD", name="Advanced Micro Devices",
        run_at=datetime(2026, 6, 16, 9, 30), reads=reads,
        summary="Daily continuation intact; weekly still basing.",
        analysis_text="CORE: Clean continuation.", is_deep=True,
    )


def test_ticker_report_body_subject_and_text():
    from swing_screener.notify.body import compose_ticker_report_body

    c = compose_ticker_report_body(_report())
    assert "AMD" in c.subject
    assert "Jun" in c.subject  # month abbreviation
    assert "Daily continuation intact; weekly still basing." in c.text
    assert "Full report attached" in c.text
    # one line per timeframe, with HA trend + RSI
    assert "1d: bullish, RSI 58" in c.text
    assert "1wk: bearish, RSI 44" in c.text


def test_ticker_report_body_html_lists_timeframes():
    from swing_screener.notify.body import compose_ticker_report_body

    c = compose_ticker_report_body(_report())
    assert "<h2>" in c.html
    assert "AMD" in c.html
    assert "1d" in c.html and "1wk" in c.html
    assert "Full report attached" in c.html
