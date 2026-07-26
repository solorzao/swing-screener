from datetime import UTC, date, datetime

from swing_screener.notify.body import (
    AlertLine,
    DigestPick,
    OrderIntentLine,
    OrderTicketLine,
    compose_digest_body,
)
from swing_screener.notify.ticker_report import TickerReport, TimeframeRead


def _ticket(**over):
    base = {"side": "long", "shares": 40, "ticker": "AMD", "limit_price": 101.0,
                "stop": 95.0, "target": 110.0, "status": "recorded", "detail": "order ticket recorded"}
    base.update(over)
    return OrderTicketLine(**base)


def test_order_ticket_renders_spec_and_status_in_body():
    picks = [DigestPick("AMD", "", "medium", "r.", score=0.9, order_ticket=_ticket())]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "Order ticket: long 40 AMD @<= 101, stop 95, target 110 — recorded" in c.text
    assert "Order ticket" in c.html  # surfaced in HTML too


def test_order_ticket_skip_renders_reason():
    picks = [DigestPick("AMD", "", "medium", "r.", score=0.9,
                        order_ticket=_ticket(status="skipped",
                                             detail="per-day notional cap: 5000 > 1000"))]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "skipped: per-day notional cap: 5000 > 1000" in c.text


def test_no_order_ticket_renders_as_before():
    picks = [DigestPick("AMD", "", "medium", "Daily continuation.", score=0.77)]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "Order ticket" not in c.text   # off picks unchanged
    assert "Order ticket" not in c.html


def test_order_intent_line_renders_in_body():
    intent = OrderIntentLine(
        conviction="high", shares=40, risk_dollars=200.0,
        edge_played="score=0.80-1.00 (forward_confirmed, +0.50R, n=40)",
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)
    picks = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Deep read.",
                        score=0.9, is_deep=True, order_intent=intent)]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=True)
    assert "Order intent: HIGH conviction" in c.text
    assert "40 shares ($200 risk)" in c.text
    assert "entry 96-101, stop 95, target 110" in c.text
    assert "edge: score=0.80-1.00" in c.text
    assert "Order intent: HIGH conviction" in c.html  # also surfaced in HTML


def test_order_intent_unconfigured_sizing_shows_r_multiples():
    intent = OrderIntentLine(
        conviction="medium", shares=0, risk_dollars=0.0, edge_played="no matching edge",
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)
    picks = [DigestPick("AMD", "", "medium", "r.", score=0.8, order_intent=intent)]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "R-multiples (sizing unconfigured)" in c.text
    assert "shares" not in c.text.split("R-multiples")[0].split("Order intent")[-1]


def test_no_order_intent_renders_as_before():
    picks = [DigestPick("AMD", "", "medium", "Daily continuation.", score=0.77)]
    c = compose_digest_body("daily", date(2026, 6, 15), picks, [], has_pdf=False)
    assert "Order intent" not in c.text   # non-insight picks unchanged


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


def test_reversal_funnel_line_shows_filtered_empty_state():
    """When the surfaced reversal list is empty but signals WERE detected, the email must
    say so -- 'No qualifying setups' alone is indistinguishable from a quiet market (the
    2026-06-28 drought went unnoticed for exactly this reason)."""
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    c = compose_digest_body("daily", date(2026, 6, 15), cont, [], has_pdf=False,
                            reversal_picks=[], reversal_funnel=(114, 17))
    assert "Reversal funnel: 114 detected · 17 confirmed · 0 surfaced" in c.text
    assert "Reversal funnel: 114 detected" in c.html


def test_reversal_funnel_line_counts_surfaced_picks():
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    rev = [DigestPick("GME", "GameStop", "short", "Oversold bounce.", score=0.7,
                      strength="confirmed")]
    c = compose_digest_body("daily", date(2026, 6, 15), cont, [], has_pdf=False,
                            reversal_picks=rev, reversal_funnel=(10, 4))
    assert "Reversal funnel: 10 detected · 4 confirmed · 1 surfaced" in c.text


def test_no_funnel_renders_as_before():
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    c = compose_digest_body("daily", date(2026, 6, 15), cont, [], has_pdf=False,
                            reversal_picks=[])
    assert "Reversal funnel" not in c.text and "Reversal funnel" not in c.html


def test_reversal_section_omitted_when_none():
    cont = [DigestPick("AMD", "Advanced Micro Devices", "medium", "Continuation.", score=0.9)]
    c = compose_digest_body("weekly", date(2026, 6, 15), cont, [], has_pdf=False)  # no reversals
    assert "Reversal Plays" not in c.text and "Reversal Plays" not in c.html


def test_continuation_section_omitted_when_parked():
    """picks=None OMITS the continuation section entirely (continuation parking, Q6 NULL)
    -- deliberately distinct from picks=[], which renders 'No qualifying setups' and
    would read like a quiet market every day (the 2026-07-01 blank-digest ambiguity).
    The reversal section must lead the body cleanly: no orphaned title, no leading
    blank line."""
    rev = [DigestPick("GME", "GameStop", "short", "Oversold bounce.", score=0.7,
                      strength="confirmed")]
    c = compose_digest_body("daily", date(2026, 6, 15), None, [], has_pdf=False,
                            reversal_picks=rev, reversal_funnel=(3, 1))
    assert "Continuation Plays" not in c.text and "Continuation Plays" not in c.html
    assert "No qualifying setups" not in c.text          # omitted, NOT an empty state
    assert c.text.startswith("Top 5 - Reversal Plays")   # reversal leads, no blank join
    assert "GME" in c.text
    assert "Reversal funnel: 3 detected · 1 confirmed · 1 surfaced" in c.text
    assert c.html.startswith("<h3>Top 5 - Reversal Plays</h3>")


def test_parked_body_opens_clean_without_leading_blank():
    """A parked weekly/monthly body (picks=None, no reversal section) whose only
    content is exit alerts and/or footers must not open with a blank line: every ''
    separator is a JOINT between blocks, rendered only when content precedes."""
    alerts = [AlertLine("MSFT", "hard", "stop", "stopped out @ 410")]
    c = compose_digest_body("weekly", date(2026, 6, 15), None, alerts, has_pdf=False,
                            health_status="health: ok")
    assert c.text.startswith("Exit alerts:")   # the first block leads, no blank prefix
    assert "\n\n\n" not in c.text              # single-blank joints everywhere
    # footer-only body (no alerts either): the health line IS the whole text
    c2 = compose_digest_body("monthly", date(2026, 6, 15), None, [], has_pdf=False,
                             health_status="health: ok")
    assert c2.text == "health: ok"


def _report():
    reads = [
        TimeframeRead(timeframe="1d", ha_trend="bullish", ema_aligned=True,
                      rsi=58.0, atr_pct=0.04, setup=None, chart_path=None),
        TimeframeRead(timeframe="1wk", ha_trend="bearish", ema_aligned=False,
                      rsi=44.0, atr_pct=0.06, setup=None, chart_path=None),
    ]
    return TickerReport(
        ticker="AMD", name="Advanced Micro Devices",
        run_at=datetime(2026, 6, 16, 9, 30, tzinfo=UTC), reads=reads,
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


def test_ticker_report_body_never_renders_nan_rsi():
    # Short frames leave the last RSI NaN (e.g. the 1mo timeframe of a young
    # listing) -- the email must show 'n/a', never a literal 'nan'.
    from dataclasses import replace

    from swing_screener.notify.body import compose_ticker_report_body

    report = _report()
    reads = [replace(report.reads[0], rsi=float("nan"))]
    c = compose_ticker_report_body(replace(report, reads=reads))
    assert "nan" not in c.text.lower() and "nan" not in c.html.lower()
    assert "RSI n/a" in c.text and "RSI n/a" in c.html
