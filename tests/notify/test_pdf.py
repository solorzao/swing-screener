import base64
import types
from datetime import UTC, datetime

from swing_screener.notify.pdf import PdfPick, build_digest_pdf
from swing_screener.notify.ticker_report import TickerReport, TimeframeRead

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _pick(ticker="AMD", chart=None, name="Advanced Micro Devices", atr_pct=0.04):
    return PdfPick(
        ticker=ticker, name=name, trade_type="medium", score=0.92, chart_path=chart,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0, risk_reward=1.5,
        quality_tier="reputable", volatility_tier="high", oversold=False, mtf_aligned=True,
        atr_pct=atr_pct,
        rationale="AMD daily uptrend intact; shallow pullback held EMA50; bullish trigger.",
    )


def test_order_intent_block_renders_when_present():
    from swing_screener.notify.pdf import build_story

    pick = PdfPick(
        ticker="AMD", name="Advanced Micro Devices", trade_type="medium", score=0.9,
        chart_path=None, entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        risk_reward=1.5, quality_tier="reputable", volatility_tier="high", oversold=False,
        mtf_aligned=True, atr_pct=0.04, is_deep=True, rationale="Strong continuation.",
        conviction="high", shares=40, risk_dollars=200.0,
        edge_played="score=0.80-1.00 (forward_confirmed, +0.50R, n=40)")
    story = build_story([pick])
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "Order intent:" in rendered
    assert "HIGH conviction" in rendered
    assert "40 shares ($200 risk)" in rendered
    assert "score=0.80-1.00" in rendered


def test_order_intent_block_absent_for_non_insight_picks():
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None)])   # no conviction -> no order-intent block
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "Order intent:" not in rendered


def test_order_ticket_block_renders_when_dispatched():
    from swing_screener.notify.pdf import build_story

    pick = PdfPick(
        ticker="AMD", name="Advanced Micro Devices", trade_type="medium", score=0.9,
        chart_path=None, entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        risk_reward=1.5, quality_tier="reputable", volatility_tier="high", oversold=False,
        mtf_aligned=True, atr_pct=0.04, rationale="Strong continuation.",
        ticket_status="recorded", ticket_detail="order ticket recorded",
        ticket_side="long", ticket_limit_price=101.0, shares=40)
    story = build_story([pick])
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "Order ticket:" in rendered
    assert "long 40 AMD" in rendered
    assert "recorded" in rendered


def test_order_ticket_block_skip_renders_reason():
    from swing_screener.notify.pdf import build_story

    pick = PdfPick(
        ticker="AMD", name="", trade_type="medium", score=0.9, chart_path=None,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0, risk_reward=1.5,
        quality_tier="reputable", volatility_tier="high", oversold=False, mtf_aligned=True,
        atr_pct=0.04, rationale="r.", ticket_status="skipped",
        ticket_detail="per-day notional cap: 5000 > 1000", ticket_side="long",
        ticket_limit_price=101.0, shares=40)
    story = build_story([pick])
    rendered = " ".join(getattr(f, "text", "") for f in story)
    # reportlab xml-escapes the detail (the ">" -> "&gt;"); the skip prefix is verbatim.
    assert "skipped: per-day notional cap: 5000" in rendered


def test_order_ticket_block_absent_when_execution_off():
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None)])   # ticket_status None -> no ticket block
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "Order ticket:" not in rendered


def test_atr_percent_row_in_levels_table():
    # ATR is surfaced as a percentage of price (not dollars) so it reads
    # consistently across high- and low-priced names.
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None, atr_pct=0.04)])
    table_rows = [row for f in story if hasattr(f, "_cellvalues") for row in f._cellvalues]
    assert ["ATR (% of price)", "4.0%"] in table_rows


def test_parked_daily_story_omits_placeholder_and_reversal_leads():
    """Continuation parking in the ATTACHMENT (Q6 NULL, 2026-07-25): ``picks=None``
    must omit BOTH the continuation story and the 'No picks.' placeholder -- a page-1
    placeholder would re-introduce in the PDF the exact quiet-market ambiguity the
    email body's omitted section avoids -- and REVERSAL PLAYS must lead directly under
    the header (no leading PageBreak, no near-empty first page)."""
    from reportlab.platypus import PageBreak

    from swing_screener.notify.pdf import build_digest_story

    story = build_digest_story(None, reversal_picks=[_pick("GME", None, name="GameStop")],
                               header="Swing Screener - Daily Picks (Jun 15, 2026)")
    texts = [f.text for f in story if hasattr(f, "text")]
    assert not any("No picks." in t for t in texts)
    assert "Daily Picks" in texts[0]        # the header still opens the PDF
    assert "REVERSAL PLAYS" in texts[1]     # ...and reversal leads right under it
    assert not any(isinstance(f, PageBreak) for f in story)  # single section: no break
    assert any("GME" in t for t in texts)


def test_empty_continuation_story_keeps_placeholder():
    """``picks=[]`` (continuation SURFACED but nothing qualified today) keeps the
    'No picks.' placeholder AND the reversal page break -- a quiet market must stay
    visibly different from a parked book, in the attachment as in the body."""
    from reportlab.platypus import PageBreak

    from swing_screener.notify.pdf import build_digest_story

    story = build_digest_story([], reversal_picks=[_pick("GME", None, name="GameStop")])
    texts = [f.text for f in story if hasattr(f, "text")]
    assert any("No picks." in t for t in texts)
    assert any(isinstance(f, PageBreak) for f in story)  # reversal still starts a page


def test_builds_nonempty_pdf_with_and_without_chart(tmp_path):
    chart = tmp_path / "AMD.png"
    chart.write_bytes(_PNG)
    out = build_digest_pdf([_pick("AMD", str(chart)), _pick("AEP", None)], tmp_path / "digest.pdf")
    assert out.exists()
    data = out.read_bytes()
    assert len(data) > 0
    assert data[:4] == b"%PDF"


def test_company_name_appears_in_pdf_flow(tmp_path):
    # the company name should flow into both the section heading and the levels
    # table; build_story exposes the flowables so we can assert on their text.
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None, name="Advanced Micro Devices")])
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "Advanced Micro Devices" in rendered
    table_rows = [row for f in story if hasattr(f, "_cellvalues") for row in f._cellvalues]
    assert ["Company", "Advanced Micro Devices"] in table_rows


def test_empty_company_name_omits_dash_in_heading(tmp_path):
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None, name="")])
    headings = [getattr(f, "text", "") for f in story]
    assert any(h.startswith("AMD &nbsp; [") for h in headings)  # no "- " with empty name


def test_deep_pick_renders_label_and_structured_rationale():
    from swing_screener.notify.pdf import build_story

    pick = PdfPick(
        ticker="AMD", name="Advanced Micro Devices", trade_type="medium", score=0.9,
        chart_path=None, entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
        risk_reward=1.5, quality_tier="reputable", volatility_tier="high", oversold=False,
        mtf_aligned=True, atr_pct=0.04, is_deep=True,
        rationale="Read: Clean continuation.\nTechnicals: Held EMA50 on the daily.\n"
                  "Risk: Broad-market wobble.",
    )
    story = build_story([pick])
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "DEEP ANALYSIS" in rendered                  # the label is present
    assert "<b>Read:</b>" in rendered                   # labels bolded -> structured
    assert "<b>Technicals:</b>" in rendered
    assert "<b>Risk:</b>" in rendered


def test_standard_pick_has_no_deep_label():
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None)])  # is_deep defaults False
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "DEEP ANALYSIS" not in rendered


def test_chart_image_preserves_aspect_and_header_builds(tmp_path):
    from swing_screener.notify.pdf import _CHART_WIDTH, _chart_image

    img = _chart_image(_PNG)  # 1x1 PNG -> square: height tracks width, not a forced 3.2in
    assert abs(img.drawWidth - _CHART_WIDTH) < 1e-6
    assert abs(img.drawHeight - _CHART_WIDTH) < 1e-6

    out = build_digest_pdf([_pick("AMD", None)], tmp_path / "h.pdf",
                           header="Swing Screener - Daily Picks (Jun 15, 2026)")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


def test_empty_picks_still_builds(tmp_path):
    out = build_digest_pdf([], tmp_path / "empty.pdf")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


def _ticker_report():
    # one timeframe with a firing setup (a tiny stub with the 4 level attrs the
    # code reads), one without -- exercises both branches of the levels block.
    setup = types.SimpleNamespace(
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)
    reads = [
        TimeframeRead(timeframe="1d", ha_trend="bullish", ema_aligned=True,
                      rsi=58.0, atr_pct=0.04, setup=setup, chart_path=None),
        TimeframeRead(timeframe="1wk", ha_trend="bearish", ema_aligned=False,
                      rsi=44.0, atr_pct=0.06, setup=None, chart_path=None),
    ]
    return TickerReport(
        ticker="AMD", name="Advanced Micro Devices",
        run_at=datetime(2026, 6, 16, 9, 30, tzinfo=UTC), reads=reads,
        summary="Daily continuation intact; weekly still basing.",
        analysis_text="CORE: Clean continuation.\n4h: Momentum building.",
        is_deep=True,
    )


def test_build_ticker_story_has_sections():
    from swing_screener.notify.pdf import build_ticker_story

    story = build_ticker_story(_ticker_report())
    assert story  # non-empty flowable list
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "AMD" in rendered
    assert rendered.count("No setup firing on this timeframe.") == 1


def test_ticker_story_never_renders_nan_read_values():
    # Short frames leave the last RSI/ATR NaN (e.g. the 1mo timeframe of a young
    # listing) -- the PDF heading must show 'n/a', never a literal 'nan'.
    from dataclasses import replace

    from swing_screener.notify.pdf import build_ticker_story

    report = _ticker_report()
    nan = float("nan")
    reads = [replace(report.reads[1], rsi=nan, atr_pct=nan)]
    story = build_ticker_story(replace(report, reads=reads))
    rendered = " ".join(getattr(f, "text", "") for f in story)
    assert "nan" not in rendered.lower()
    assert "RSI n/a" in rendered and "ATR n/a" in rendered


def test_build_ticker_report_pdf_writes_file(tmp_path):
    from swing_screener.notify.pdf import build_ticker_report_pdf

    out = build_ticker_report_pdf(_ticker_report(), tmp_path / "AMD.pdf")
    assert out.exists()
    data = out.read_bytes()
    assert len(data) > 0
    assert data[:4] == b"%PDF"


def test_blob_chart_fetch_failure_logs_ticker_and_key(monkeypatch, caplog):
    """A missing/aged-out blob chart degrades CHARTLESS but never silently: the
    warning names the ticker and the blob key so an aged-out container is
    diagnosable from the log (2026-07-17 audit M4a)."""
    import logging

    from swing_screener.notify import pdf as pdf_mod
    from swing_screener.notify.pdf import build_story

    monkeypatch.setattr(pdf_mod, "blob_enabled", lambda: True)

    def boom(key):
        raise FileNotFoundError(f"no blob {key}")

    monkeypatch.setattr(pdf_mod, "download_bytes", boom)
    with caplog.at_level(logging.WARNING, logger="swing_screener.notify.pdf"):
        story = build_story([_pick(chart="20260615/AMD_1d_20260615.png")])
    assert story  # the pick still renders, chartless
    assert any("AMD" in r.message and "20260615/AMD_1d_20260615.png" in r.message
               for r in caplog.records), caplog.records


def test_ticker_report_blob_chart_failure_logs_ticker_and_key(monkeypatch, caplog):
    """Same posture on the ticker-report path: the per-timeframe blob fetch failure
    names the ticker and the key instead of a bare pass."""
    import dataclasses
    import logging

    from swing_screener.notify import pdf as pdf_mod
    from swing_screener.notify.pdf import build_ticker_story

    monkeypatch.setattr(pdf_mod, "blob_enabled", lambda: True)

    def boom(key):
        raise FileNotFoundError(f"no blob {key}")

    monkeypatch.setattr(pdf_mod, "download_bytes", boom)
    report = _ticker_report()
    reads = [dataclasses.replace(report.reads[0], chart_path="20260616/AMD_1d.png"),
             report.reads[1]]
    report = dataclasses.replace(report, reads=reads)
    with caplog.at_level(logging.WARNING, logger="swing_screener.notify.pdf"):
        story = build_ticker_story(report)
    assert story
    assert any("AMD" in r.message and "20260616/AMD_1d.png" in r.message
               for r in caplog.records), caplog.records
