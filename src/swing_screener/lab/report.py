"""Render lab payloads into the deterministic facts block the Opus deep
analysis receives. Pure text over payload dicts -- the LLM posture invariant
holds here: every number the model sees is computed by this engine; the prompt
tells it these are ground truth and it must never invent or alter levels.
"""

from typing import Any

_TF_LABEL = {"4h": "4-hour", "1d": "daily", "1wk": "weekly", "1mo": "monthly"}


def _fmt(v: object, nd: int = 2) -> str:
    if not isinstance(v, (int, float)):
        return "n/a"
    return f"{v:.{nd}f}"


def _last(values: list[Any]) -> float | None:
    """The final value, or None when it is null -- never a stale walk-back."""
    if not values:
        return None
    v = values[-1]
    return float(v) if isinstance(v, (int, float)) else None


def _ha_streak(candles: list[dict[str, Any]]) -> tuple[str, int]:
    """Colour of the last HA candle and how many consecutive bars share it."""
    def colour(c: dict[str, Any]) -> str | None:
        o, cl = c.get("ha_o"), c.get("ha_c")
        if not isinstance(o, (int, float)) or not isinstance(cl, (int, float)):
            return None
        return "green" if cl >= o else "red"

    last = colour(candles[-1]) if candles else None
    if last is None:
        return "n/a", 0
    n = 0
    for c in reversed(candles):
        if colour(c) != last:
            break
        n += 1
    return last, n


def _ema_lines(payload: dict[str, Any]) -> str:
    close = payload.get("last_close")
    parts: list[str] = []
    order: list[tuple[int, float]] = []
    for span_s, series in payload.get("emas", {}).items():
        v = _last(series)
        if v is None:
            parts.append(f"EMA{span_s}=n/a (insufficient history)")
            continue
        parts.append(f"EMA{span_s}={_fmt(v)}")
        order.append((int(span_s), v))
    line = ", ".join(parts)
    if len(order) >= 2:
        stacked = " > ".join(
            str(s) for s, _ in sorted(order, key=lambda x: x[1], reverse=True)
        )
        line += f"; stack (highest first): {stacked}"
    if isinstance(close, (int, float)) and order:
        above = [str(s) for s, v in order if close > v]
        below = [str(s) for s, v in order if close <= v]
        line += (f"; close above EMA {', '.join(above) or 'none'}"
                 f", at/below EMA {', '.join(below) or 'none'}")
    return line


def _macd_line(payload: dict[str, Any]) -> str:
    md = payload.get("macd", {})
    m, s, h = (_last(md.get(k, [])) for k in ("macd", "signal", "hist"))
    if m is None or s is None or h is None:
        return "MACD(12,26,9): n/a (insufficient history)"
    hist_series = [v for v in md.get("hist", []) if isinstance(v, (int, float))]
    slope = "n/a"
    if len(hist_series) >= 2:
        slope = "rising" if hist_series[-1] > hist_series[-2] else "falling"
    return (f"MACD(12,26,9): macd={_fmt(m, 3)}, signal={_fmt(s, 3)}, "
            f"hist={_fmt(h, 3)} ({slope}); macd {'above' if m > s else 'below'} "
            f"signal, {'above' if m > 0 else 'below'} zero")


def _volume_line(payload: dict[str, Any]) -> str:
    vols = [c.get("v") for c in payload.get("candles", [])]
    nums = [float(v) for v in vols if isinstance(v, (int, float))]
    if not nums:
        return "volume: n/a"
    last = nums[-1]
    window = nums[-20:]
    avg = sum(window) / len(window)
    ratio = f", {last / avg:.2f}x the 20-bar average" if avg > 0 else ""
    return f"volume: last bar {last:,.0f}, 20-bar avg {avg:,.0f}{ratio}"


def _levels_lines(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    levels = payload.get("levels", {})
    for side in ("resistance", "support"):
        rows = levels.get(side, [])
        if rows:
            body = ", ".join(
                f"{_fmt(r.get('price'))} ({r.get('touches')} touches)" for r in rows
            )
        else:
            body = "none detected in window"
        out.append(f"{side}: {body}")
    fib = payload.get("fib")
    if fib:
        pts = ", ".join(
            f"{lv['ratio'] * 100:.1f}%={_fmt(lv.get('price'))}"
            for lv in fib.get("levels", [])
            if lv.get("ratio") not in (0.0, 1.0)
        )
        out.append(
            f"fibonacci retracement of the {fib.get('direction')}-swing "
            f"{_fmt(fib.get('low'))} to {_fmt(fib.get('high'))}: {pts}"
        )
    else:
        out.append("fibonacci: n/a (degenerate window)")
    return out


def lab_facts_text(payloads: dict[str, dict[str, Any]]) -> str:
    """One facts block per timeframe, in lab order, ready for the Opus prompt."""
    blocks: list[str] = []
    for tf in ("4h", "1d", "1wk", "1mo"):
        p = payloads.get(tf)
        label = _TF_LABEL.get(tf, tf)
        if p is None:
            blocks.append(f"== {tf} ({label}) ==\nunavailable (fetch failed)")
            continue
        colour, streak = _ha_streak(p.get("candles", []))
        lines = [
            f"== {tf} ({label}) -- {p.get('bar_count')} bars, as of {p.get('as_of')} ==",
            f"last close: {_fmt(p.get('last_close'))}",
            f"heiken ashi: last candle {colour}, {streak} consecutive",
            _ema_lines(p),
            _macd_line(p),
            _volume_line(p),
            *_levels_lines(p),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
