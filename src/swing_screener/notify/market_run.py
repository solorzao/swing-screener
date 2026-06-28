"""Weekly macro "Market Weather" report entrypoint.

Fetch the market series -> gather deterministic facts -> analyze (LLM if enabled, else the
deterministic read) -> email the report -> persist a MarketReport row. All I/O is behind
injectable seams (``fetch`` / Anthropic ``client`` / ``smtp_send``) so tests stay offline.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import anthropic
import pandas as pd
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.config_secrets import get_secret
from swing_screener.data.fetch import fetch_bars
from swing_screener.db.models import MarketReport
from swing_screener.db.session import get_engine
from swing_screener.notify.market_analysis import (
    MarketAnalysis,
    analyze_market_deep,
    deterministic_market_analysis,
)
from swing_screener.notify.market_body import compose_market_body
from swing_screener.notify.transport import resolve_sender
from swing_screener.pipeline.market import MarketFacts, gather_market_facts

log = logging.getLogger(__name__)


def _default_fetch(cache_dir: Path) -> Callable[[str], pd.DataFrame | None]:
    def fetch(ticker: str) -> pd.DataFrame | None:
        return fetch_bars(ticker, "1d", cache_dir=cache_dir)
    return fetch


def _resolve_recipient(to: str | None) -> str | None:
    if to:
        return to
    for key in ("DIGEST_RECIPIENT", "GMAIL_ADDRESS"):
        try:
            val = get_secret(key)
        except Exception:  # noqa: BLE001 -- a missing secret must not crash the report
            val = None
        if val:
            return val
    return None


def _persist(session: Session, facts: MarketFacts, analysis: MarketAnalysis) -> None:
    session.add(MarketReport(
        run_date=facts.as_of, ha_alignment=facts.ha_alignment,
        flipped=any(h.flipped for h in facts.ha.values()),
        spy_vs_200dma=facts.spy_vs_200dma, vol_bucket=facts.vol_bucket,
        vix=facts.vix, vix_rank=facts.vix_rank, vix_spike=facts.vix_spike,
        ten_year=facts.ten_year, three_month=facts.three_month,
        yield_inverted=facts.yield_inverted, bond_trend=facts.bond_trend,
        is_deep=analysis.is_deep, core=analysis.core[:512], report=analysis.report,
        created_at=datetime.now(UTC),
    ))
    session.commit()


def run_market_report(
    *, db_url: str, cache_dir: Path = Path(".cache"), to: str | None = None,
    smtp_send: Callable[..., None] | None = None, client: anthropic.Anthropic | None = None,
    fetch: Callable[[str], pd.DataFrame | None] | None = None,
    cfg: StrategyConfig | None = None, run_date: date | None = None,
) -> MarketFacts | None:
    """Fetch -> gather facts -> analyze -> email -> persist. Returns the MarketFacts, or None if
    SPY is unavailable. The report is the LLM read when ``cfg.market_report_enabled`` else the
    deterministic facts read; it always persists and (with a recipient) emails."""
    cfg = cfg or StrategyConfig()
    fetch = fetch or _default_fetch(cache_dir)

    spy = fetch("SPY")
    if spy is None or len(spy) == 0:
        log.error("market report: no SPY data; skipping")
        return None
    facts = gather_market_facts(
        spy_daily=spy, vix_daily=fetch("^VIX"), tlt_daily=fetch("TLT"),
        tnx_daily=fetch("^TNX"), irx_daily=fetch("^IRX"), cfg=cfg, as_of=run_date,
    )

    if cfg.market_report_enabled:
        analysis = analyze_market_deep(
            facts, client=client, model=cfg.market_model,
            reasoning=cfg.market_reasoning, max_searches=cfg.market_max_searches,
        )
    else:
        analysis = deterministic_market_analysis(facts)

    body = compose_market_body(facts, analysis)
    recipient = _resolve_recipient(to)
    if recipient:
        send = smtp_send or resolve_sender()
        send(to=recipient, subject=body.subject, text=body.text, html=body.html)
    else:
        log.warning("market report: no recipient; persisting without emailing")

    engine = get_engine(db_url)
    try:
        with Session(engine) as s:
            _persist(s, facts, analysis)
    finally:
        engine.dispose()
    return facts
