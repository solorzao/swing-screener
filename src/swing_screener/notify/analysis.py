"""Claude-written rationale for a top pick.

The model only *narrates* the deterministic facts of a signal — it must not
invent setups or levels. The Anthropic client is an injectable seam so tests
pass a fake and never hit the network. If the API call fails for any reason we
fall back to a deterministic rationale built purely from the facts, so the
nightly pipeline never blocks on the LLM.
"""

import logging
from dataclasses import dataclass

import anthropic

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
        f"reward/risk of {facts.risk_reward:.1f}R. RSI is {facts.rsi:.0f} on an ATR "
        f"of {facts.atr:g}."
    )


def _prompt(facts: SignalFacts) -> str:
    return (
        "Write a rationale for this signal using only these facts:\n"
        f"- Ticker: {facts.ticker}\n"
        f"- Timeframe: {facts.timeframe}\n"
        f"- Trade type: {facts.trade_type}\n"
        f"- Score: {facts.score:.2f}\n"
        f"- Multi-timeframe aligned: {facts.mtf_aligned}\n"
        f"- Quality tier: {facts.quality_tier}\n"
        f"- Volatility tier: {facts.volatility_tier}\n"
        f"- Oversold: {facts.oversold}\n"
        f"- Trigger close: {facts.trigger_close:g}\n"
        f"- ATR: {facts.atr:g}\n"
        f"- RSI: {facts.rsi:.0f}\n"
        f"- Entry zone: {facts.entry_floor:g} to {facts.entry_ceiling:g}\n"
        f"- Stop: {facts.stop:g}\n"
        f"- Target: {facts.target:g}\n"
        f"- Reward/risk: {facts.risk_reward:.1f}R\n"
    )


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
        client = client or anthropic.Anthropic()  # inside try: a key-at-construction
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
