"""Render the weekly macro "Market Weather" report into an email.

Pure (no I/O). Takes the deterministic ``MarketFacts`` + the analyst's ``MarketAnalysis`` and
produces an :class:`EmailContent` (subject + text + minimal HTML). The subject front-loads the
actionable state -- the HA alignment plus any fresh flip / VIX spike / yield inversion -- so it
reads at a glance in an inbox.
"""

import html as _html

from swing_screener.notify.body import EmailContent
from swing_screener.notify.market_analysis import MarketAnalysis, facts_block
from swing_screener.pipeline.market import MarketFacts

_ALIGN_LABEL = {"aligned_bull": "BULL aligned", "aligned_bear": "BEAR aligned", "mixed": "MIXED"}


def _subject_flags(f: MarketFacts) -> str:
    flags = []
    if any(h.flipped for h in f.ha.values()):
        flags.append("FLIP")
    if f.vix_spike:
        flags.append("VIX spike")
    if f.yield_inverted:
        flags.append("inverted")
    return f" [{', '.join(flags)}]" if flags else ""


def compose_market_body(facts: MarketFacts, analysis: MarketAnalysis) -> EmailContent:
    """Subject: 'Market Weather <date> — <alignment>[ flags]'. Text: the analyst report followed
    by the deterministic facts block (ground truth). HTML: the same, lightly formatted."""
    subject = (f"Market Weather {facts.as_of} — "
               f"{_ALIGN_LABEL.get(facts.ha_alignment, facts.ha_alignment)}{_subject_flags(facts)}")
    block = facts_block(facts)
    text = f"{analysis.report}\n\n{'-' * 40}\n{block}"
    if not analysis.is_deep:
        text += "\n\n(LLM analysis was unavailable; this is the deterministic facts report.)"
    html = (f"<h2>Market Weather — {facts.as_of}</h2>"
            f"<pre style='white-space:pre-wrap;font-family:inherit'>{_html.escape(analysis.report)}</pre>"
            f"<hr><pre style='white-space:pre-wrap;color:#555'>{_html.escape(block)}</pre>")
    return EmailContent(subject=subject, text=text, html=html)
