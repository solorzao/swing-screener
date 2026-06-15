import base64

from swing_screener.notify.pdf import PdfPick, build_digest_pdf

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _pick(ticker="AMD", chart=None):
    return PdfPick(
        ticker=ticker, trade_type="medium", score=0.92, chart_path=chart,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0, risk_reward=1.5,
        quality_tier="reputable", volatility_tier="high", oversold=False, mtf_aligned=True,
        rationale="AMD daily uptrend intact; shallow pullback held EMA50; bullish trigger.",
    )


def test_builds_nonempty_pdf_with_and_without_chart(tmp_path):
    chart = tmp_path / "AMD.png"
    chart.write_bytes(_PNG)
    out = build_digest_pdf([_pick("AMD", str(chart)), _pick("AEP", None)], tmp_path / "digest.pdf")
    assert out.exists()
    data = out.read_bytes()
    assert len(data) > 0
    assert data[:4] == b"%PDF"


def test_empty_picks_still_builds(tmp_path):
    out = build_digest_pdf([], tmp_path / "empty.pdf")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"
