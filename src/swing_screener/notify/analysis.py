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
class SignalAnalysis:
    core_reason: str
    rationale: str


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

# Reasoning effort -> extended-thinking budget (tokens). 0 disables thinking.
# Thinking tokens bill as OUTPUT, so this is the main reasoning<->cost lever.
_REASONING_BUDGET = {"none": 0, "low": 2000, "medium": 6000, "high": 12000}
_DEEP_ANSWER_TOKENS = 1500  # answer room on top of the thinking budget

_DEEP_SYSTEM = (
    "You are an equity research assistant for a swing trader. You are given a "
    "price chart image, a set of DETERMINISTIC signal facts already computed by a "
    "rules engine (treat the entry zone, stop, and target as ground truth -- never "
    "change them), company fundamentals, and recent news. Use the web_search tool "
    "to check current market sentiment and industry/sector trends for this ticker. "
    "Then write an analytical read of the setup that weighs the technicals (chart + "
    "facts) against the fundamentals, news, sentiment, and sector trend.\n\n"
    "Rules: never invent or alter price levels. Cite sources for external claims. "
    "Be balanced -- name the risks, not just the bull case. This is informational "
    "analysis, NOT financial advice or a recommendation to trade.\n\n"
    "Reply in exactly this shape: a first line beginning with 'CORE: ' then one "
    "concise sentence on why this setup stands out (or its key risk), then a blank "
    "line, then a 3-6 sentence rationale referencing the timeframe, the chart "
    "structure, the entry zone/stop/target, and what the fundamentals/news/"
    "sentiment add."
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
        client = client or anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        budget = _REASONING_BUDGET.get(reasoning, _REASONING_BUDGET["high"])
        kwargs: dict = {
            "model": model,
            "max_tokens": budget + _DEEP_ANSWER_TOKENS,
            "system": _DEEP_SYSTEM,
            "messages": [
                {"role": "user", "content": _deep_user_content(facts, chart_bytes, context_text)}
            ],
        }
        if budget > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if web_search:
            kwargs["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}
            ]
        resp = client.messages.create(**kwargs)  # type: ignore[arg-type]
        text, sources = _extract_text_and_citations(resp)
        if not text.strip():
            raise ValueError("empty model response")
        analysis = _parse(text, facts)
        if sources:
            analysis = SignalAnalysis(
                core_reason=analysis.core_reason,
                rationale=analysis.rationale + _format_sources(sources),
            )
        return analysis
    except Exception:
        log.warning(
            "deep analysis failed for %s %s; using deterministic fallback",
            facts.ticker, facts.timeframe, exc_info=True,
        )
        return SignalAnalysis(
            core_reason=_deterministic_core(facts),
            rationale=_deterministic_rationale(facts),
        )
