from swing_screener.charts.render import render_chart
from swing_screener.config import StrategyConfig
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.entry_zone import compute_zone
from swing_screener.signals.frame import build_frame


def _firing(bars):
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def test_render_chart_creates_nonempty_png(bars, tmp_path):
    cfg = StrategyConfig()
    frame = build_frame(_firing(bars), cfg)
    ctx = detect_last_bar(frame, cfg)
    assert ctx is not None
    zone = compute_zone(ctx.trigger_close, ctx.atr, ctx.swing_low, cfg)
    assert zone is not None

    out = render_chart(frame, ctx, zone, tmp_path / "AAPL_1d.png")
    assert out.exists()
    assert out.stat().st_size > 0
