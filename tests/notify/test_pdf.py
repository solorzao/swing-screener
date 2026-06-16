import base64

from swing_screener.notify.pdf import PdfPick, build_digest_pdf

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


def test_atr_percent_row_in_levels_table():
    # ATR is surfaced as a percentage of price (not dollars) so it reads
    # consistently across high- and low-priced names.
    from swing_screener.notify.pdf import build_story

    story = build_story([_pick("AMD", None, atr_pct=0.04)])
    table_rows = [row for f in story if hasattr(f, "_cellvalues") for row in f._cellvalues]
    assert ["ATR (% of price)", "4.0%"] in table_rows


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
