"""Weekly macro "Market Weather" report entrypoint.

Fetch the market series -> gather deterministic facts -> analyze (LLM if enabled, else the
deterministic read) -> email the report -> persist a MarketReport row. All I/O is behind
injectable seams (``fetch`` / Anthropic ``client`` / ``smtp_send``) so tests stay offline.
"""

import argparse
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import anthropic
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from swing_screener.pipeline.run import _migrate_with_retry
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)


def _default_fetch(cache_dir: Path) -> Callable[[str], pd.DataFrame | None]:
    def fetch(ticker: str) -> pd.DataFrame | None:
        return fetch_bars(ticker, "1d", cache_dir=cache_dir)
    return fetch


def _resolve_recipient(to: str | None) -> str | None:
    if to:
        return to
    # DIGEST_TO is the Key-Vault-backed recipient the rest of the system uses (notify.run,
    # notify.ondemand); GMAIL_ADDRESS is the local-dev fallback.
    for key in ("DIGEST_TO", "GMAIL_ADDRESS"):
        try:
            val = get_secret(key)
        except Exception:  # noqa: BLE001 -- a missing secret must not crash the report
            val = None
        if val:
            return val
    return None


def _db_float(v: float | None) -> float | None:
    """NaN -> None at the DB boundary. SQL Server rejects NaN floats at the wire (TDS
    8023 -- the 2026-07-05 market-weather failure, a holiday-padded NaN close), and
    NaN-as-data violates the honest-numbers rule regardless: an unavailable input is
    an explicit NULL, never NaN. ``v != v`` is the NaN test."""
    return None if v is None or v != v else v  # noqa: PLR0124 -- NaN check


def _persist(session: Session, facts: MarketFacts, analysis: MarketAnalysis) -> None:
    # Every float column passes _db_float: the fact gatherers already fall back to the
    # last valid print, so this layer only fires if a future computation regresses.
    session.add(MarketReport(
        run_date=facts.as_of, ha_alignment=facts.ha_alignment,
        flipped=any(h.flipped for h in facts.ha.values()),
        spy_vs_200dma=facts.spy_vs_200dma, vol_bucket=facts.vol_bucket,
        vix=_db_float(facts.vix), vix_rank=_db_float(facts.vix_rank),
        vix_spike=facts.vix_spike,
        ten_year=_db_float(facts.ten_year), three_month=_db_float(facts.three_month),
        yield_inverted=facts.yield_inverted, bond_trend=facts.bond_trend,
        vix_term_ratio=_db_float(facts.vix_term_ratio),
        vix_backwardation=facts.vix_backwardation,
        credit_chg_4w=_db_float(facts.credit_chg_4w),
        credit_pctile=_db_float(facts.credit_pctile),
        cyc_def_trend=facts.cyc_def_trend, cyc_def_chg_4w=_db_float(facts.cyc_def_chg_4w),
        breadth_trend=facts.breadth_trend, breadth_chg_4w=_db_float(facts.breadth_chg_4w),
        recession_prob=_db_float(facts.recession_prob),
        is_deep=analysis.is_deep, core=analysis.core[:512], report=analysis.report,
        # Spend visibility (E6): the one deep call's APPROXIMATE list-price cost. NULL
        # when no billed call was captured (deterministic/fallback path) -- never $0.
        est_cost_usd=(
            _db_float(analysis.usage.est_cost_usd) if analysis.usage is not None else None
        ),
        created_at=datetime.now(UTC),
    ))
    session.commit()


def run_market_report(
    *, db_url: str, cache_dir: Path = Path(".cache"), to: str | None = None,
    smtp_send: Callable[..., None] | None = None, client: anthropic.Anthropic | None = None,
    fetch: Callable[[str], pd.DataFrame | None] | None = None,
    migrate_fn: Callable[[str], None] | None = None,
    cfg: StrategyConfig | None = None, run_date: date | None = None,
) -> MarketFacts | None:
    """Fetch -> gather facts -> (idempotency check) -> analyze -> PERSIST -> email.

    Returns the MarketFacts, or None if SPY is unavailable OR a report for the as-of date
    already exists (the Sunday UTC cron pair can double-fire on DST days, and a crashed
    run retries -- prod once got two identical rows + two emails for one run_date).
    The check runs BEFORE the billable analysis, and the row persists BEFORE the email:
    a send crash leaves the report behind so the retry skips cleanly instead of
    re-analyzing and double-mailing. The unique index on ``market_reports.run_date``
    backstops the rare concurrent-replica race (the loser skips its email too)."""
    cfg = cfg or StrategyConfig()
    fetch = fetch or _default_fetch(cache_dir)

    spy = fetch("SPY")
    if spy is None or len(spy) == 0:
        log.error("market report: no SPY data; skipping")
        return None
    facts = gather_market_facts(
        spy_daily=spy, vix_daily=fetch("^VIX"), tlt_daily=fetch("TLT"),
        tnx_daily=fetch("^TNX"), irx_daily=fetch("^IRX"),
        vix3m_daily=fetch("^VIX3M"), hyg_daily=fetch("HYG"), lqd_daily=fetch("LQD"),
        xly_daily=fetch("XLY"), xlp_daily=fetch("XLP"), rsp_daily=fetch("RSP"),
        cfg=cfg, as_of=run_date,
    )

    # Alembic owns the Azure SQL schema (get_engine does NOT create_all there), so self-migrate --
    # the Sunday run can precede a fresh migration and must not assume another job seeded the table.
    # Gated on mssql like every other entrypoint: locally alembic lives in the [azure] extra and
    # the create_all-born local.db is unstamped, so an unconditional migrate crashes sqlite runs.
    if db_url.startswith("mssql"):
        (migrate_fn or _migrate_with_retry)(db_url)
    engine = get_engine(db_url)
    try:
        with Session(engine) as s:
            already = s.scalars(select(MarketReport).where(
                MarketReport.run_date == facts.as_of)).first()
        if already is not None:
            log.info("market report for %s already persisted; skipping (idempotent re-run)",
                     facts.as_of)
            return None

        # The LLM runs only when BOTH switches agree: StrategyConfig.market_report_enabled
        # (the code-level constant; config.py stays env-free by design) AND the
        # SWING_MARKET_REPORT env gate (settings; absent = on, so today's behavior is
        # unchanged). Env is read at call time via load_settings, the same seam
        # notify.run uses for its deep-analysis gate -- ops can turn the weekly bill
        # off without a code change. Either off path still persists + emails the
        # deterministic read, after the idempotency check above, at $0.
        if cfg.market_report_enabled and load_settings().market_report_enabled:
            analysis = analyze_market_deep(
                facts, client=client, model=cfg.market_model,
                reasoning=cfg.market_reasoning, max_searches=cfg.market_max_searches,
            )
        else:
            analysis = deterministic_market_analysis(facts)

        try:
            with Session(engine) as s:
                _persist(s, facts, analysis)  # PERSIST FIRST (see docstring)
        except IntegrityError:  # lost a concurrent-replica race; the winner also emails
            log.info("market report for %s persisted by a concurrent run; skipping email",
                     facts.as_of)
            return None

        body = compose_market_body(facts, analysis)
        recipient = _resolve_recipient(to)
        if recipient:
            send = smtp_send or resolve_sender()
            send(to=recipient, subject=body.subject, text=body.text, html=body.html)
        else:
            log.warning("market report: no recipient; persisted without emailing")
    finally:
        engine.dispose()
    return facts


def main() -> None:
    """CLI entry for the weekly Market Weather report (schedule via cron / a CI workflow)."""
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Send the weekly macro Market Weather report.")
    parser.add_argument("--db", default=settings.db_url)
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    parser.add_argument("--to", default=None, help="recipient (defaults to the configured digest address)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    facts = run_market_report(db_url=args.db, cache_dir=args.cache_dir, to=args.to)
    if facts is not None:
        log.info("market report: alignment=%s vix_rank=%s inverted=%s",
                 facts.ha_alignment, facts.vix_rank, facts.yield_inverted)


if __name__ == "__main__":
    main()
