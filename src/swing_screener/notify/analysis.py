"""Claude-written rationale for a top pick.

The model only *narrates* the deterministic facts of a signal — it must not
invent setups or levels. The Anthropic client is an injectable seam so tests
pass a fake and never hit the network. If the API call fails for any reason we
fall back to a deterministic rationale built purely from the facts, so the
nightly pipeline never blocks on the LLM.
"""

import base64
import logging
from dataclasses import dataclass

import anthropic

from swing_screener.config_secrets import get_secret
from swing_screener.notify.ticker_report import TickerReport, TimeframeRead
from swing_screener.pipeline.insight import _CONVICTIONS

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

_SYSTEM = (
    "You are a trading assistant that writes a short rationale for a swing-trade "
    "signal. You are given a fixed set of deterministic facts about one signal. "
    "Narrate ONLY those facts. Never invent price levels, indicators, news, or "
    "claims that are not in the facts. Do not give financial advice or predict "
    "outcomes.\n\n"
    "Reply in exactly this shape: a first line beginning with 'CORE: ' followed "
    "by one concise sentence summarising why this signal stands out, then a blank "
    "line, then a 2-4 sentence rationale that references the timeframe, the entry "
    "zone, the stop, and the target."
)


@dataclass(frozen=True)
class SignalFacts:
    ticker: str
    timeframe: str
    trade_type: str
    score: float
    mtf_aligned: bool
    quality_tier: str
    volatility_tier: str
    oversold: bool
    trigger_close: float
    atr: float
    rsi: float
    entry_floor: float
    entry_ceiling: float
    stop: float
    target: float

    @property
    def risk_reward(self) -> float:
        """Reward-to-risk ratio, guarded against a non-positive denominator."""
        denom = self.entry_ceiling - self.stop
        if denom <= 0:
            return 0.0
        return (self.target - self.entry_ceiling) / denom

    @property
    def atr_pct(self) -> float:
        """ATR as a fraction of the trigger price (e.g. 0.023 == 2.3%).

        ``atr`` is stored in dollars; expressing it relative to price makes the
        volatility signal comparable across high- and low-priced names (a $4 ATR
        means very different things on a $40 vs a $400 stock). Guarded against a
        non-positive price.
        """
        if self.trigger_close <= 0:
            return 0.0
        return self.atr / self.trigger_close


@dataclass(frozen=True)
class Usage:
    """Token + web-search spend captured from one model call.

    ``est_cost_usd`` is an APPROXIMATE list-price estimate (see ``_MODEL_PRICES``)
    used only for a safety cap and cost visibility -- not billing-accurate. None on
    the deterministic/fallback paths, where no model call was made.
    """

    input_tokens: int
    output_tokens: int
    web_searches: int
    est_cost_usd: float


@dataclass(frozen=True)
class SignalAnalysis:
    core_reason: str
    rationale: str
    is_deep: bool = False  # True only when the Opus deep path actually produced this
    usage: Usage | None = None  # token spend; None on the deterministic/fallback path


def _deterministic_core(facts: SignalFacts) -> str:
    return (
        f"{facts.ticker} {facts.timeframe} {facts.trade_type} continuation setup "
        f"(score {facts.score:.2f})."
    )


def _deterministic_rationale(facts: SignalFacts) -> str:
    alignment = "multi-timeframe aligned" if facts.mtf_aligned else "single-timeframe"
    return (
        f"{facts.ticker} triggered a {facts.timeframe} pullback-continuation signal "
        f"({alignment}, {facts.quality_tier} quality, {facts.volatility_tier} "
        f"volatility). Entry zone is {facts.entry_floor:g}-{facts.entry_ceiling:g} "
        f"with a stop at {facts.stop:g} and a target at {facts.target:g}, a "
        f"reward/risk of {facts.risk_reward:.1f}R. RSI is {facts.rsi:.0f} with an ATR "
        f"of {facts.atr_pct:.1%} of price."
    )


def _facts_lines(facts: SignalFacts) -> str:
    """The deterministic fact bullets, shared by the simple and deep prompts."""
    return (
        f"- Ticker: {facts.ticker}\n"
        f"- Timeframe: {facts.timeframe}\n"
        f"- Trade type: {facts.trade_type}\n"
        f"- Score: {facts.score:.2f}\n"
        f"- Multi-timeframe aligned: {facts.mtf_aligned}\n"
        f"- Quality tier: {facts.quality_tier}\n"
        f"- Volatility tier: {facts.volatility_tier}\n"
        f"- Oversold: {facts.oversold}\n"
        f"- Trigger close: {facts.trigger_close:g}\n"
        f"- ATR (% of price): {facts.atr_pct:.1%}\n"
        f"- RSI: {facts.rsi:.0f}\n"
        f"- Entry zone: {facts.entry_floor:g} to {facts.entry_ceiling:g}\n"
        f"- Stop: {facts.stop:g}\n"
        f"- Target: {facts.target:g}\n"
        f"- Reward/risk: {facts.risk_reward:.1f}R\n"
    )


def _prompt(facts: SignalFacts) -> str:
    return "Write a rationale for this signal using only these facts:\n" + _facts_lines(facts)


def _parse(text: str, facts: SignalFacts) -> SignalAnalysis:
    """Split a model reply into (core_reason, rationale).

    The first line starting with ``CORE:`` is the one-line core reason; the rest
    (after the blank line) is the rationale. If there is no ``CORE:`` line, fall
    back to a deterministic core and use the whole text as the rationale.
    """
    lines = text.splitlines()
    core: str | None = None
    rest_start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("CORE:"):
            core = line.split("CORE:", 1)[1].strip()
            rest_start = i + 1
            break

    if core is None:
        return SignalAnalysis(
            core_reason=_deterministic_core(facts),
            rationale=text.strip(),
        )

    rationale = "\n".join(lines[rest_start:]).strip()
    return SignalAnalysis(core_reason=core, rationale=rationale)


def analyze_signal(
    facts: SignalFacts, *, client: anthropic.Anthropic | None = None
) -> SignalAnalysis:
    """Narrate a signal's facts into a rationale via Claude.

    ``client`` is an injectable seam: pass a fake in tests so no network call is
    made. On any failure, return a deterministic rationale built from the facts
    so the pipeline never blocks on the LLM.
    """
    try:
        # inside try: a missing key at construction (or a failing call) falls
        # back to the deterministic rationale. get_secret resolves env first,
        # then Key Vault; the injected client seam bypasses this entirely.
        client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        resp = client.messages.create(           # SDK must also fall back gracefully
            model=MODEL,
            max_tokens=600,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _prompt(facts)}],
        )
        text: str = next(
            (
                getattr(b, "text", "")
                for b in resp.content
                if getattr(b, "type", None) == "text"
            ),
            "",
        )
        return _parse(text, facts)
    except Exception:
        log.warning(
            "claude analysis failed for %s %s; using deterministic fallback",
            facts.ticker,
            facts.timeframe,
            exc_info=True,
        )
        return SignalAnalysis(
            core_reason=_deterministic_core(facts),
            rationale=_deterministic_rationale(facts),
        )


# --- Deep analysis: an Opus "analyst" with chart vision + web search ----------

# Reasoning effort -> Anthropic output_config.effort (opus-4.8+ adaptive thinking).
# "none"/unknown sends no thinking at all. Thinking bills as OUTPUT, so this is the
# main reasoning<->cost lever. NOTE: opus-4.8 rejects the older
# thinking={"type":"enabled","budget_tokens":N} shape -- it wants adaptive + effort.
_REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high"}
# Per-effort output cap (thinking + answer). Generous; effort, not this, sets depth.
_REASONING_MAX_TOKENS = {"none": 2000, "low": 6000, "medium": 10000, "high": 16000}

_DEEP_SYSTEM = (
    "You are an equity research assistant for a swing trader. You receive a price "
    "chart image, DETERMINISTIC signal facts already computed by a rules engine "
    "(treat the entry zone, stop, and target as ground truth -- never change them), "
    "company fundamentals, and recent news. Use the web_search tool to check "
    "current market sentiment and industry/sector trends.\n\n"
    "Output ONLY the finished analysis. Do NOT narrate your process, mention "
    "searching or 'looking', or include any preamble, filler, or meta-commentary. "
    "Never invent or alter price levels. Cite sources for external claims. Be "
    "concise and balanced. This is informational analysis, NOT financial advice.\n\n"
    "Format your reply EXACTLY as these labelled lines (one per line, each 1-2 "
    "sentences, no bullet characters, no extra sections):\n"
    "CORE: <one sentence -- why this setup stands out, or its key risk>\n"
    "Read: <the overall take>\n"
    "Technicals: <from the chart + facts; reference the timeframe and entry "
    "zone/stop/target>\n"
    "Fundamentals: <from the financials>\n"
    "Sentiment: <from recent news + web search; cite>\n"
    "Risk: <the single most important risk>"
)


def _deep_prompt(facts: SignalFacts, context_text: str) -> str:
    return (
        f"Signal for {facts.ticker} ({facts.timeframe}, {facts.trade_type}). The "
        "image above is its annotated chart.\n\n"
        "Deterministic signal facts (ground truth -- do not change levels):\n"
        f"{_facts_lines(facts)}\n"
        f"{context_text}\n\n"
        "Use web_search for current sentiment + industry/sector trends, then write "
        "the analysis."
    )


def _deep_user_content(facts: SignalFacts, chart_bytes: bytes | None,
                       context_text: str) -> list[dict]:
    """Build the user content array: image FIRST (best practice), then the text."""
    content: list[dict] = []
    if chart_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(chart_bytes).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": _deep_prompt(facts, context_text)})
    return content


def _extract_text_and_citations(resp: object) -> tuple[str, list[tuple[str, str]]]:
    """Concatenate text blocks and collect (title, url) web-search citations.

    server_tool_use / web_search_tool_result blocks carry the mechanics; the prose
    and its citations live on the text blocks. A search that errors just yields no
    citations -- the model still answers, with less fresh data.
    """
    parts: list[str] = []
    sources: list[tuple[str, str]] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        parts.append(getattr(block, "text", "") or "")
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url:
                sources.append((getattr(c, "title", None) or url, url))
    return "".join(parts), sources


def _format_sources(sources: list[tuple[str, str]]) -> str:
    """Render up to 6 unique citations as a trailing 'Sources:' list."""
    seen: dict[str, str] = {}
    for title, url in sources:
        seen.setdefault(url, title)  # first title wins, dedup by url
    rows = list(seen.items())[:6]
    return "\n\nSources:\n" + "\n".join(f"- {title}: {url}" for url, title in rows)


# --- Token-spend capture: APPROXIMATE list-price estimate for the safety cap ----
#
# Per-model (input_$/MTok, output_$/MTok). Source: Anthropic published list prices
# (claude-api skill model table, cached 2026-06-04; matches the public pricing page).
# This is an APPROXIMATE estimate -- it ignores prompt-cache discounts and image
# tokens -- used only for cost visibility and the Task-2 safety cap, never billing.
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}
# Web search list price: $10 per 1,000 searches == $0.01 per search (Anthropic docs,
# web-search tool "Usage and pricing"). Also approximate.
_WEB_SEARCH_COST_USD = 0.01


def _count_web_searches(usage: object) -> int:
    """Web searches the API reports for this call, best-effort -> 0.

    The Messages API surfaces the count at ``usage.server_tool_use.web_search_requests``
    (an int). Anything missing/None/non-int yields 0 rather than crashing the analyst.
    """
    stu = getattr(usage, "server_tool_use", None)
    n = getattr(stu, "web_search_requests", None)
    return n if isinstance(n, int) and n >= 0 else 0


def _capture_usage(resp: object, model: str) -> Usage | None:
    """Build a ``Usage`` from ``resp.usage``, or None if it's missing/None.

    GUARDED: a response with no ``usage`` (or ``usage is None``) returns None so the
    capture never crashes the analyst. ``est_cost_usd`` is the approximate list-price
    estimate (unknown model -> token term 0); web searches always add their per-call
    cost so a search-heavy unpriced model still shows nonzero spend.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    in_tok = getattr(usage, "input_tokens", None) or 0
    out_tok = getattr(usage, "output_tokens", None) or 0
    searches = _count_web_searches(usage)
    in_price, out_price = _MODEL_PRICES.get(model, (0.0, 0.0))
    est = (
        in_tok / 1_000_000 * in_price
        + out_tok / 1_000_000 * out_price
        + searches * _WEB_SEARCH_COST_USD
    )
    return Usage(
        input_tokens=int(in_tok),
        output_tokens=int(out_tok),
        web_searches=searches,
        est_cost_usd=est,
    )


def _create_message(client: anthropic.Anthropic, kwargs: dict) -> object:
    """messages.create, retrying once WITHOUT the reasoning params if the model
    rejects them. Models differ on the thinking API (opus-4.8 wants adaptive +
    output_config.effort; older models want budget_tokens), so on a config mismatch
    we retry plain -- still a real model analysis, not the deterministic narrator.
    """
    try:
        return client.messages.create(**kwargs)
    except anthropic.BadRequestError as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("thinking", "output_config", "effort")) and (
            "thinking" in kwargs or "output_config" in kwargs
        ):
            plain = {k: v for k, v in kwargs.items() if k not in ("thinking", "output_config")}
            return client.messages.create(**plain)
        raise


class EmptyAnalysisError(ValueError):
    """The model returned no usable text. Carries the captured Usage so callers
    can still charge billed-but-failed calls against spend ceilings (Task E3b)."""

    def __init__(self, usage: Usage | None) -> None:
        super().__init__("empty model response")
        self.usage = usage


def _analyst_call(
    *, system: str, content: list[dict], client: anthropic.Anthropic | None,
    model: str, reasoning: str, max_searches: int, web_search: bool,
) -> tuple[str, list[tuple[str, str]], Usage | None]:
    """One place that owns the analyst call scaffold: client construction, kwargs
    assembly, thinking/effort wiring, web-search tool config, _create_message,
    text+citation extraction, usage capture, and the empty-response check.
    Raises on any failure; each analyst keeps its own deterministic fallback."""
    client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    kwargs: dict = {
        "model": model,
        "max_tokens": _REASONING_MAX_TOKENS.get(reasoning, 16000),
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    effort = _REASONING_EFFORT.get(reasoning)
    if effort is not None:  # opus-4.8+ adaptive thinking; "none"/unknown omits it
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}
    if web_search:
        kwargs["tools"] = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
        ]
    resp = _create_message(client, kwargs)
    usage = _capture_usage(resp, model)
    text, sources = _extract_text_and_citations(resp)
    if not text.strip():
        raise EmptyAnalysisError(usage)
    return text, sources, usage


def analyze_signal_deep(
    facts: SignalFacts, *, chart_bytes: bytes | None = None, context_text: str = "",
    client: anthropic.Anthropic | None = None, model: str = "claude-opus-4-8",
    reasoning: str = "high", max_searches: int = 4, web_search: bool = True,
) -> SignalAnalysis:
    """Opus analyst: reads the chart image + facts + fundamentals/news, web-searches
    sentiment/trends, and weighs it all. Falls back to the deterministic rationale on
    ANY failure (missing key, API/tool error, empty reply) so the digest never blocks.
    """
    try:
        text, sources, usage = _analyst_call(
            system=_DEEP_SYSTEM,
            content=_deep_user_content(facts, chart_bytes, context_text),
            client=client, model=model, reasoning=reasoning,
            max_searches=max_searches, web_search=web_search,
        )
        analysis = _parse(text, facts)
        rationale = analysis.rationale + (_format_sources(sources) if sources else "")
        return SignalAnalysis(
            core_reason=analysis.core_reason, rationale=rationale, is_deep=True,
            usage=usage,
        )
    except Exception:
        log.warning(
            "deep analysis failed for %s %s; using deterministic fallback",
            facts.ticker, facts.timeframe, exc_info=True,
        )
        return SignalAnalysis(
            core_reason=_deterministic_core(facts),
            rationale=_deterministic_rationale(facts),
        )


# --- On-demand: one Opus call over a whole multi-timeframe ticker picture ------

_TICKER_SYSTEM = (
    "You are an equity research assistant for a swing trader. You receive ONE "
    "ticker's per-timeframe Heiken-Ashi facts already computed by a rules engine "
    "(HA trend, EMA alignment, RSI, ATR%, and -- when a setup is firing -- its entry "
    "zone, stop, and target) plus annotated chart images for those timeframes. Treat "
    "any firing setup's entry/stop/target as ground truth: NEVER invent or alter "
    "price levels. Use the web_search tool to check current market sentiment and "
    "industry/sector trends.\n\n"
    "Output ONLY the finished analysis. Do NOT narrate your process, mention "
    "searching or 'looking', or include any preamble, filler, or meta-commentary. "
    "Cite sources for external claims. Be concise and balanced. This is "
    "informational analysis, NOT financial advice.\n\n"
    "Format your reply EXACTLY as these labelled lines (one per line, each 1-2 "
    "sentences, no bullet characters, no extra sections):\n"
    "CORE: <one-sentence overall stance across the timeframes>\n"
    "4h: <note>\n"
    "1d: <note>\n"
    "1wk: <note>\n"
    "1mo: <note>\n"
    "Setups: <which timeframes are firing + their levels, or 'none'>\n"
    "Risk: <the single most important risk>\n"
    "Watch: <key levels to watch>"
)


def _read_line(read: TimeframeRead) -> str:
    """One deterministic fact line per timeframe read (entry/stop/target if firing)."""
    line = (
        f"- {read.timeframe}: HA {read.ha_trend}, EMA "
        f"{'aligned' if read.ema_aligned else 'not aligned'}, "
        f"RSI {read.rsi:.0f}, ATR {read.atr_pct:.1%}"
    )
    if read.setup is not None:
        s = read.setup
        line += (
            f" -- firing {s.play_type} (entry {s.entry_floor:g}-{s.entry_ceiling:g}, "
            f"stop {s.stop:g}, target {s.target:g})"
        )
    return line


def _ticker_prompt(report: TickerReport) -> str:
    """The per-timeframe deterministic facts block (ground truth) for the user turn."""
    body = "\n".join(_read_line(r) for r in report.reads) or "- (no timeframes available)"
    return (
        f"Ticker: {report.ticker} ({report.name}). The images above are its annotated "
        "charts, one per timeframe in low->high order.\n\n"
        "Per-timeframe Heiken-Ashi facts (ground truth -- do not change any levels):\n"
        f"{body}\n\n"
        "Use web_search for current sentiment + industry/sector trends, then write "
        "the multi-timeframe analysis."
    )


def _ticker_user_content(report: TickerReport, charts: list[bytes]) -> list[dict]:
    """User content array: every chart image FIRST (best practice), then the text."""
    content: list[dict] = []
    for chart_bytes in charts:
        if not chart_bytes:
            continue
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(chart_bytes).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": _ticker_prompt(report)})
    return content


def _ticker_fallback(report: TickerReport) -> tuple[str, str, bool]:
    """Deterministic multi-timeframe summary built purely from the reads."""
    summary = f"{report.ticker}: multi-timeframe read"
    lines: list[str] = []
    for r in report.reads:
        line = f"{r.timeframe}: {r.ha_trend}, RSI {r.rsi:.0f}, ATR {r.atr_pct:.1%}"
        if r.setup is not None:
            s = r.setup
            line += (
                f", firing (entry {s.entry_floor:g}-{s.entry_ceiling:g}, "
                f"stop {s.stop:g}, target {s.target:g})"
            )
        lines.append(line)
    return summary, "\n".join(lines), False


def analyze_ticker_deep(
    report: TickerReport, *, charts: list[bytes] | None = None, context_text: str = "",
    client: anthropic.Anthropic | None = None, model: str = "claude-opus-4-8",
    reasoning: str = "high", max_searches: int = 4, web_search: bool = True,
) -> tuple[str, str, bool]:
    """Return (summary, analysis_text, is_deep). ONE Opus call over the whole
    multi-timeframe picture: the per-TF deterministic facts + chart images. Falls
    back to a deterministic multi-TF summary on ANY failure so the worker still
    produces a report.
    """
    try:
        content = _ticker_user_content(report, charts or [])
        if context_text:
            content.append({"type": "text", "text": context_text})
        # usage is captured by the helper but DISCARDED here: this path still
        # returns the bare (summary, analysis_text, is_deep) tuple (E3c widens it).
        text, sources, _usage = _analyst_call(
            system=_TICKER_SYSTEM, content=content,
            client=client, model=model, reasoning=reasoning,
            max_searches=max_searches, web_search=web_search,
        )
        summary = next(
            (
                line.split("CORE:", 1)[1].strip()
                for line in text.splitlines()
                if line.strip().startswith("CORE:")
            ),
            f"{report.ticker}: multi-timeframe read",
        )
        analysis_text = text.strip() + (_format_sources(sources) if sources else "")
        return summary, analysis_text, True
    except Exception:
        log.warning(
            "ticker deep analysis failed for %s; using deterministic fallback",
            report.ticker, exc_info=True,
        )
        return _ticker_fallback(report)


# --- Conviction nudge: the analyst MOVES the deterministic grade, bounded +-1 ---
#
# North Star #9: the analyst is a learning participant, not a narrator. Given the
# code-owned BASELINE conviction (pipeline.insight.conviction_baseline) it may
# genuinely move the grade -- but only one step along the ordered _CONVICTIONS
# scale, and that bound is CLAMPED in code (never trusted to the model). On ANY
# failure it falls back to the baseline + the deterministic rationale, so the
# per-pick insight engine never blocks on the LLM.

_CONVICTION_SYSTEM = (
    "You are a conviction analyst for a swing trader. You receive a DETERMINISTIC "
    "baseline conviction grade (already computed by a rules engine from the "
    "strategy playbook), the strategy playbook itself, the signal's deterministic "
    "facts, and external context. Weigh it all and decide the final conviction.\n\n"
    "The conviction scale is ordered: avoid < low < medium < high. You may MOVE "
    "the grade by AT MOST one step (+-1) from the baseline -- never jump further "
    "(e.g. never go from 'high' to 'avoid'). Give a one-line reason for any change, "
    "or say 'agree with baseline' if you keep it. NEVER invent or alter price "
    "levels. Use the web_search tool to check current sentiment / sector trends "
    "that bear on the thesis; cite sources for external claims.\n\n"
    "Output ONLY the finished assessment. Do NOT narrate your process or include "
    "preamble. Format your reply EXACTLY like this:\n"
    "CONVICTION: <avoid|low|medium|high>\n"
    "REASON: <one line -- why you moved it, or 'agree with baseline'>\n"
    "<then the insight prose: where this pick sits vs the playbook, the external "
    "context that bears on the thesis, and the single biggest risk>"
)


@dataclass(frozen=True)
class ConvictionResult:
    conviction: str  # final, AFTER the code clamp -- always within +-1 of baseline
    nudge_reason: str
    insight: str
    is_deep: bool = False  # True only when the Opus path actually produced this
    usage: Usage | None = None  # token spend; None on the deterministic/fallback path


def _conviction_prompt(facts: SignalFacts, baseline: str, playbook_text: str,
                       context_text: str) -> str:
    return (
        f"Signal for {facts.ticker} ({facts.timeframe}, {facts.trade_type}). The "
        "image above (if any) is its annotated chart.\n\n"
        f"Deterministic BASELINE conviction (the starting point): {baseline}\n\n"
        f"Strategy playbook (the edge this baseline keys on):\n{playbook_text}\n\n"
        "Deterministic signal facts (ground truth -- do not change levels):\n"
        f"{_facts_lines(facts)}\n"
        f"{context_text}\n\n"
        "Decide the final conviction (at most +-1 from the baseline), give your "
        "one-line reason, then write the insight."
    )


def _conviction_user_content(facts: SignalFacts, baseline: str, playbook_text: str,
                             chart_bytes: bytes | None, context_text: str) -> list[dict]:
    """User content array: chart image FIRST (best practice), then the text block."""
    content: list[dict] = []
    if chart_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(chart_bytes).decode("ascii"),
            },
        })
    content.append({
        "type": "text",
        "text": _conviction_prompt(facts, baseline, playbook_text, context_text),
    })
    return content


def _clamp_conviction(parsed: str, baseline: str, *, max_step: int = 1) -> str:
    """Clamp the model's grade to +-``max_step`` of the baseline, in CODE -- never trusting
    the model to respect the bound. An unrecognized grade falls back to the baseline.

    ``max_step`` defaults to 1 (today's hard ±1 clamp -- byte-identical). A CALIBRATED play
    type earns ``max_step=2`` (``analytics.calibration.max_conviction_step``); the bound moves
    SIZING only, never a price level, and is recomputed every run (reversible).
    """
    parsed = parsed.strip().lower()
    base_idx = _CONVICTIONS.index(baseline)
    if parsed not in _CONVICTIONS:
        return baseline
    idx = _CONVICTIONS.index(parsed)
    idx = max(base_idx - max_step, min(base_idx + max_step, idx))
    return _CONVICTIONS[idx]


def _parse_conviction(text: str, baseline: str, *, max_step: int = 1) -> tuple[str, str, str]:
    """Split a reply into (clamped_conviction, reason, insight). The first
    ``CONVICTION:`` and ``REASON:`` lines are the grade + reason; everything else is
    the insight prose. The grade is clamped to +-``max_step`` of the baseline in code."""
    raw = baseline
    reason = "agree with baseline"
    rest: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("CONVICTION:"):
            raw = stripped.split(":", 1)[1].strip()
        elif stripped.upper().startswith("REASON:"):
            reason = stripped.split(":", 1)[1].strip()
        else:
            rest.append(line)
    insight = "\n".join(rest).strip()
    return _clamp_conviction(raw, baseline, max_step=max_step), reason, insight


def analyze_conviction(
    facts: SignalFacts, *, baseline: str, playbook_text: str, context_text: str = "",
    chart_bytes: bytes | None = None, client: anthropic.Anthropic | None = None,
    model: str = "claude-opus-4-8", reasoning: str = "high", max_searches: int = 4,
    web_search: bool = True, max_step: int = 1,
) -> ConvictionResult:
    """Let the Opus analyst MOVE the deterministic baseline conviction (bounded +-``max_step``,
    clamped in code) and write the per-pick insight. Falls back to the baseline +
    the deterministic rationale on ANY failure (missing key, API/tool error, empty
    reply, unparseable) so the insight engine never blocks on the LLM.

    ``max_step`` defaults to 1 (the hard ±1 clamp -- byte-identical to today). A play type
    whose conviction CALIBRATES earns ``max_step=2`` (``calibration.max_conviction_step``):
    the analyst's influence GROWS with its track record (North Star #9), but only over SIZING
    -- it still cannot touch a price level -- and the bound is recomputed every run (reversible).

    web_search is wired in (optional, default-on) so the analyst can pull live
    sentiment/sector context that genuinely bears on the thesis -- the point of the
    "learning participant" seam; citations get appended to the insight.
    """
    try:
        text, sources, usage = _analyst_call(
            system=_CONVICTION_SYSTEM,
            content=_conviction_user_content(
                facts, baseline, playbook_text, chart_bytes, context_text
            ),
            client=client, model=model, reasoning=reasoning,
            max_searches=max_searches, web_search=web_search,
        )
        conviction, reason, insight = _parse_conviction(text, baseline, max_step=max_step)
        insight += _format_sources(sources) if sources else ""
        return ConvictionResult(
            conviction=conviction, nudge_reason=reason, insight=insight, is_deep=True,
            usage=usage,
        )
    except Exception:
        log.warning(
            "conviction analysis failed for %s %s; using baseline + deterministic rationale",
            facts.ticker, facts.timeframe, exc_info=True,
        )
        return ConvictionResult(
            conviction=baseline,
            nudge_reason="(baseline; analyst unavailable)",
            insight=_deterministic_rationale(facts),
            is_deep=False,
        )
