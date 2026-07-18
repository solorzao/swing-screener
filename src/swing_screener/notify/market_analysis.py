"""Macro market analyst: a weekly "Market Weather" read written by Opus.

The market-broad sibling of ``notify.analysis``. Instead of one ticker, the analyst receives the
deterministic MARKET facts (SPY multi-timeframe Heiken-Ashi agreement, VIX regime, yield-curve
inversion, bond trend) as ground truth and web-searches the rest (Shiller CAPE, Fear & Greed, the
2y curve, economic news). Falls back to a deterministic facts report on ANY failure so the weekly
report never blocks. The Anthropic client is an injectable seam so tests never hit the network.
"""

import logging
import math
from dataclasses import dataclass, replace

import anthropic

from swing_screener.config_secrets import get_secret
from swing_screener.notify.analysis import (
    _REASONING_EFFORT,
    _REASONING_MAX_TOKENS,
    Usage,
    _capture_usage,
    _create_message,
    _extract_text_and_citations,
    _format_sources,
)
from swing_screener.pipeline.market import MarketFacts

log = logging.getLogger(__name__)

_MARKET_SYSTEM = (
    "You are a macro market strategist writing a weekly market-health read for a swing trader. "
    "You receive DETERMINISTIC market facts (SPY Heiken-Ashi across monthly/weekly/daily and "
    "whether they AGREE, the VIX percentile regime, the 10y/3m yield-curve inversion, and the "
    "bond trend) -- treat these numbers as ground truth and NEVER alter them. Use the web_search "
    "tool to fill in the CURRENT Shiller CAPE, the CNN Fear & Greed index, the 2y / fuller yield "
    "curve, and the week's major economic news.\n\n"
    "Output ONLY the finished analysis. Begin your reply IMMEDIATELY with \"CORE:\" -- no preamble, "
    "no search/tool narration (NEVER write things like \"I'll search for...\"), no commentary before "
    "or after the labelled lines. Cite sources for external claims. Be concise and balanced. This is "
    "informational market analysis, NOT financial advice and NOT a stock recommendation.\n\n"
    "Format your reply EXACTLY as these labelled lines (one per line, each 1-2 sentences, no "
    "bullet characters, no extra sections):\n"
    "CORE: <one sentence -- the overall market stance right now>\n"
    "Regime: <SPY multi-timeframe HA agreement + 200DMA; call out any fresh flip / divergence>\n"
    "Volatility: <VIX level + percentile regime; flag a panic spike>\n"
    "Rates: <yield curve / inversion + the bond trend>\n"
    "Valuation: <Shiller CAPE vs history>\n"
    "Sentiment: <Fear & Greed + the week's economic news; cite>\n"
    "Rotation: <any sector / style / risk-on-off rotation>\n"
    "Risk: <the single biggest risk to watch>\n"
    "Watch: <key levels / events for the week ahead>\n"
    "Bottom line: <2-4 sentences that SYNTHESIZE everything above into your overall read and the "
    "practical market-level posture it argues for -- how defensive vs aggressive to lean, what to "
    "favor or avoid, and what single development would change your mind. This is your genuine "
    "analytical view; keep it at the MARKET level (no individual-security buy/sell calls or price "
    "targets).>"
)


@dataclass(frozen=True)
class MarketAnalysis:
    core: str            # one-line market stance
    report: str          # the full labelled report (+ a Sources: list when cited)
    is_deep: bool = False  # True only when the Opus path actually produced this
    usage: Usage | None = None


def _fmt(x: float | None, nd: int = 2) -> str:
    return f"{x:.{nd}f}" if x is not None and math.isfinite(x) else "n/a"


def _fin(x: float | None) -> float | None:
    """A finite float or None -- a NaN/inf fact gets the same treatment as missing."""
    return x if x is None or math.isfinite(x) else None


def _drop_non_finite(f: MarketFacts) -> MarketFacts:
    """Non-finite fact floats -> None before rendering. PR #104 hardened the computation
    layer; this is the rendering-layer backstop so a stray NaN/inf can never reach the
    analyst prompt (or the deterministic fallback report) as a literal 'nan'."""
    return replace(
        f,
        vix=_fin(f.vix), vix_rank=_fin(f.vix_rank),
        ten_year=_fin(f.ten_year), three_month=_fin(f.three_month),
        recession_prob=_fin(f.recession_prob), vix_term_ratio=_fin(f.vix_term_ratio),
        credit_chg_4w=_fin(f.credit_chg_4w), credit_pctile=_fin(f.credit_pctile),
        cyc_def_chg_4w=_fin(f.cyc_def_chg_4w), breadth_chg_4w=_fin(f.breadth_chg_4w),
    )


def facts_block(f: MarketFacts) -> str:
    """Render the deterministic market facts as a text block (prompt + fallback report)."""
    f = _drop_non_finite(f)
    rows = [f"- SPY Heiken-Ashi (monthly/weekly/daily): {f.ha_alignment_note} "
            f"[alignment: {f.ha_alignment}]"]
    for tf in ("1mo", "1wk", "1d"):
        h = f.ha.get(tf)
        if h is not None:
            flip = " (FRESH FLIP)" if h.flipped else ""
            rows.append(f"  - {tf}: {h.color}{flip}, {h.bars_in_state} bars in state")
    if f.spy_vs_200dma:
        rows.append(f"- SPY vs 200DMA: {f.spy_vs_200dma}; volatility regime: {f.vol_bucket or 'n/a'}")
    if f.vix is not None:
        vix = f"- VIX: {f.vix:.1f}"
        if f.vix_rank is not None:
            vix += f" (percentile rank {f.vix_rank:.0f}{'; PANIC SPIKE' if f.vix_spike else ''})"
        rows.append(vix)
    if f.ten_year is not None or f.three_month is not None:
        inv = ("INVERTED (3m > 10y)" if f.yield_inverted else "normal"
               if f.yield_inverted is not None else "n/a")
        rows.append(f"- Treasury yields: 10y {_fmt(f.ten_year)}, 3m {_fmt(f.three_month)} -> {inv}")
    if f.bond_trend:
        rows.append(f"- Bonds (TLT weekly HA): {f.bond_trend}")
    if f.recession_prob is not None:
        rows.append(f"- Recession probability (NY-Fed 10y-3m probit): {f.recession_prob:.0f}%")
    if f.vix_term_ratio is not None:
        shape = "BACKWARDATION (acute stress)" if f.vix_backwardation else "contango (normal)"
        rows.append(f"- VIX term structure (VIX/VIX3M): {f.vix_term_ratio:.2f} -> {shape}")
    if f.credit_pctile is not None:
        chg = f", {f.credit_chg_4w:+.1f}% 4wk" if f.credit_chg_4w is not None else ""
        state = "spreads WIDENING" if (f.credit_chg_4w or 0.0) < 0 else "spreads stable/tightening"
        rows.append(f"- HY credit (HYG/LQD): percentile {f.credit_pctile:.0f}{chg} -> {state}")
    if f.cyc_def_trend:
        chg = f" ({f.cyc_def_chg_4w:+.1f}% 4wk)" if f.cyc_def_chg_4w is not None else ""
        rows.append(f"- Rotation cyclicals vs defensives (XLY/XLP weekly HA): {f.cyc_def_trend}{chg}")
    if f.breadth_trend:
        chg = f" ({f.breadth_chg_4w:+.1f}% 4wk)" if f.breadth_chg_4w is not None else ""
        rows.append(f"- Breadth participation (RSP/SPY equal-weight, weekly HA): {f.breadth_trend}{chg}")
    return f"Deterministic market facts (as of {f.as_of}):\n" + "\n".join(rows)


_STANCE = {
    "aligned_bull": "Risk-on -- SPY is aligned bullish across monthly, weekly, and daily.",
    "aligned_bear": "Risk-off -- SPY is aligned bearish across monthly, weekly, and daily.",
    "mixed": "Transitional -- SPY timeframes diverge; watch for a trend shift.",
}


def _deterministic_report(f: MarketFacts) -> str:
    """The no-LLM fallback report, built purely from the facts."""
    stance = _STANCE.get(f.ha_alignment, "Market read.")
    flags = "".join(s for s in (" VIX panic spike." if f.vix_spike else "",
                                " Yield curve inverted." if f.yield_inverted else "") if s)
    return (f"CORE: {stance}{flags}\n\n{facts_block(f)}\n\n"
            "(Deterministic fallback -- LLM market analysis unavailable.)")


def _market_prompt(f: MarketFacts) -> str:
    return (f"Weekly market snapshot as of {f.as_of}.\n\n{facts_block(f)}\n\n"
            "Use web_search for the current Shiller CAPE, the CNN Fear & Greed index, the 2y/10y "
            "yield curve, and the week's major economic news, then write the market read.")


def deterministic_market_analysis(facts: MarketFacts) -> MarketAnalysis:
    """The no-LLM market read (used when the report is not LLM-enabled)."""
    det = _deterministic_report(facts)
    return MarketAnalysis(core=_core_line(det), report=det, is_deep=False)


def _strip_preamble(text: str) -> str:
    """Drop any leading narration before the first 'CORE:' (models with web_search sometimes emit
    'I'll search...' -- occasionally on the SAME line as CORE -- before the labelled output)."""
    idx = text.find("CORE:")
    return text[idx:].lstrip() if idx > 0 else text


def _core_line(text: str) -> str:
    """The CORE sentence -- found anywhere (it can be glued to a leaked preamble), to end of line."""
    idx = text.find("CORE:")
    if idx < 0:
        return "Market read"
    return text[idx + len("CORE:"):].split("\n", 1)[0].strip()


def analyze_market_deep(
    facts: MarketFacts, *, client: anthropic.Anthropic | None = None,
    model: str = "claude-opus-4-8", reasoning: str = "high", max_searches: int = 6,
    web_search: bool = True,
) -> MarketAnalysis:
    """Opus macro analyst: weighs the deterministic market facts, web-searches the macro context,
    and writes the weekly read. Falls back to the deterministic facts report on ANY failure."""
    try:
        client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        kwargs: dict = {
            "model": model,
            "max_tokens": _REASONING_MAX_TOKENS.get(reasoning, 16000),
            "system": _MARKET_SYSTEM,
            "messages": [{"role": "user", "content": _market_prompt(facts)}],
        }
        effort = _REASONING_EFFORT.get(reasoning)
        if effort is not None:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": effort}
        if web_search:
            kwargs["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
            ]
        resp = _create_message(client, kwargs)
        text, sources = _extract_text_and_citations(resp)
        if not text.strip():
            raise ValueError("empty model response")
        text = _strip_preamble(text)
        report = text.strip() + (_format_sources(sources) if sources else "")
        return MarketAnalysis(core=_core_line(text), report=report, is_deep=True,
                              usage=_capture_usage(resp, model))
    except Exception:
        log.warning("market deep analysis failed; using deterministic fallback", exc_info=True)
        det = _deterministic_report(facts)
        return MarketAnalysis(core=_core_line(det), report=det, is_deep=False)
