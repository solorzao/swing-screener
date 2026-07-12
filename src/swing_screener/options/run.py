"""CLI for the GEX options lab: plan / settle / analyze / import-robinhood.

Run as ``python -m swing_screener.options.run <cmd>``. Defaults are env-first
(``load_settings()``), so a container picks up SWING_DB_URL / SWING_CACHE_DIR and
a bare local run points at local.db / .cache. Phase 1 is local-only: no Azure job
wires these up (see docs/modules/gex-lab.md).
"""

import argparse
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.data.fetch import fetch_bars
from swing_screener.db.models import OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options import broker_import
from swing_screener.options.chain import (
    ChainSnapshot,
    LiquidityReport,
    assess_liquidity,
    snapshot_chain,
)
from swing_screener.options.config import GexConfig
from swing_screener.options.gex import GexLevels, compute_gex
from swing_screener.options.plan import DayPlan, build_plan, save_snapshot
from swing_screener.options.settle import SettleResult, settle_open_trades
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)

Snapshotter = Callable[[str, GexConfig], ChainSnapshot]
DailyBars = Callable[[str], pd.DataFrame]
BarsFetcher = Callable[[str], pd.DataFrame]


def _default_snapshotter(ticker: str, cfg: GexConfig) -> ChainSnapshot:
    return snapshot_chain(ticker, cfg=cfg)


def _default_daily(ticker: str) -> pd.DataFrame:
    bars = fetch_bars(ticker, "1d", cache_dir=load_settings().cache_dir)
    if bars is None or bars.empty:
        raise RuntimeError(f"no daily bars for {ticker}")
    return bars


def _default_5m(ticker: str) -> pd.DataFrame:
    # Post-close, the day-keyed cache is safe: all session 5m bars are complete
    # (see docs/modules/gex-lab.md -- the completed-bar invariant holds for settle).
    bars = fetch_bars(ticker, "5m", cache_dir=load_settings().cache_dir, period="5d")
    if bars is None or bars.empty:
        raise RuntimeError(f"no 5m bars for {ticker}")
    return bars


def _analyze(ticker: str, cfg: GexConfig, snapshotter: Snapshotter) -> tuple[
    ChainSnapshot, GexLevels, LiquidityReport
]:
    snap = snapshotter(ticker, cfg)
    liq = assess_liquidity(snap.frame, snap.spot, cfg)
    levels = compute_gex(snap.frame, snap.spot, snap.asof.date(), cfg)
    return snap, levels, liq


def run_plan(
    session: Session, *, cfg: GexConfig,
    snapshotter: Snapshotter | None = None, daily_bars: DailyBars | None = None,
) -> list[DayPlan]:
    """Build and persist a morning GEX map + day plan for each watchlist ticker.

    Per-ticker failure isolation: one bad chain never sinks the rest of the plan.
    """
    snap_fn = snapshotter or _default_snapshotter
    daily_fn = daily_bars or _default_daily
    plans: list[DayPlan] = []
    for ticker in cfg.watchlist:
        try:
            snap, levels, liq = _analyze(ticker, cfg, snap_fn)
            save_snapshot(session, underlying=ticker, ts=snap.asof,
                          levels=levels, thin=liq.thin)
            daily = daily_fn(ticker)
            plans.append(build_plan(ticker, daily["close"], levels, cfg))
        except Exception:
            log.exception("plan failed for %s", ticker)
    return plans


def run_analyze(
    ticker: str, *, cfg: GexConfig, snapshotter: Snapshotter | None = None,
    save: bool = False, session: Session | None = None,
) -> tuple[GexLevels, LiquidityReport]:
    """Ad-hoc GEX map for any optionable ticker. Persists only when ``save`` and a
    session are given."""
    snap, levels, liq = _analyze(ticker, cfg, snapshotter or _default_snapshotter)
    if save and session is not None:
        save_snapshot(session, underlying=ticker, ts=snap.asof, levels=levels, thin=liq.thin)
    return levels, liq


def run_settle(
    session: Session, *, cfg: GexConfig, bars_fetcher: BarsFetcher | None = None,
) -> SettleResult:
    """Settle every open options-lab trade against its underlying's completed 5m bars."""
    fetch = bars_fetcher or _default_5m
    underlyings = list(session.scalars(
        select(OptionPaperTrade.underlying)
        .where(OptionPaperTrade.account == "options-lab", OptionPaperTrade.status == "open")
        .distinct()
    ))
    bars_by: dict[str, pd.DataFrame] = {}
    for underlying in underlyings:
        try:
            bars_by[underlying] = fetch(underlying)
        except Exception:
            log.exception("no 5m bars for %s; its trades stay open", underlying)
    return settle_open_trades(session, bars_by_underlying=bars_by)


def run_import(session: Session, csv_path: str | Path, *, tag_all: str | None = None) -> str:
    """Parse a Robinhood activity CSV, store its fills, and print the episode table.

    Commits nothing unless ``tag_all`` in {"gex", "other"} (the cockpit review grid
    is the real per-episode tagging surface).
    """
    text = Path(csv_path).read_text(encoding="utf-8")
    fills = broker_import.parse_activity_csv(text)
    stats = broker_import.store_fills(session, fills)
    episodes = broker_import.pair_episodes(fills)
    lines = [
        f"fills: {stats.added} added, {stats.skipped} duplicate; episodes: {len(episodes)}",
        _episode_table(episodes),
    ]
    if tag_all in {"gex", "other"}:
        tags = {e.import_key: tag_all for e in episodes if e.status == "closed"}
        n = broker_import.commit_episodes(session, episodes, tags)
        lines.append(f"committed {n} closed episodes as '{tag_all}'")
    else:
        lines.append("review in the cockpit Import panel to tag episodes (or --tag-all)")
    return "\n".join(lines)


def _episode_table(episodes: list[broker_import.Episode]) -> str:
    if not episodes:
        return "  (no option episodes)"
    rows = ["  contract                span                  x   P&L        status"]
    for e in episodes:
        span = f"{e.opened_on}->{e.closed_on or 'open'}"
        pnl = f"{e.pnl:+.2f}" if e.pnl is not None else "   -"
        flag = " REVIEW" if e.needs_review else ""
        rows.append(f"  {e.occ_symbol:<22} {span:<21} {e.contracts:>3} {pnl:>9}  "
                    f"{e.status}{flag}")
    return "\n".join(rows)


def _print_levels(ticker: str, levels: GexLevels, liq: LiquidityReport) -> None:
    print(f"{ticker}  spot={levels.spot:.2f}  regime={levels.regime}")
    print(f"  call wall={levels.call_wall}  put wall={levels.put_wall}  "
          f"gamma flip={levels.gamma_flip}")
    if liq.thin:
        print(f"  THIN CHAIN -- levels unreliable: {'; '.join(liq.reasons)}")
    print("  cross-check against a free public GEX dashboard before trusting these.")


def _resolve_db_url(cli_db: str | None) -> str:
    """Explicit ``--db`` wins over ``load_settings()``; refuse throwaway SQLite in a
    cloud context (KEY_VAULT_URL / SWING_REQUIRE_DB set) -- copied from pipeline.run."""
    db_url = cli_db if cli_db is not None else load_settings().db_url
    in_cloud = bool(os.environ.get("KEY_VAULT_URL") or os.environ.get("SWING_REQUIRE_DB"))
    if in_cloud and db_url.startswith("sqlite"):
        raise RuntimeError(
            "refusing to run against throwaway SQLite in a cloud context; "
            "set SWING_DB_URL to the Azure SQL URL"
        )
    return db_url


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="GEX options lab CLI (local, paper-only).")
    parser.add_argument("--db", default=None)
    parser.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="build the morning GEX map + day plan for the watchlist")
    sub.add_parser("settle", help="settle open lab trades from completed 5m bars")
    p_an = sub.add_parser("analyze", help="ad-hoc GEX map for any optionable ticker")
    p_an.add_argument("ticker")
    p_an.add_argument("--save", action="store_true", help="persist the snapshot")
    p_im = sub.add_parser("import-robinhood", help="import a Robinhood activity CSV")
    p_im.add_argument("csv")
    p_im.add_argument("--tag-all", choices=["gex", "other"], default=None,
                      help="commit all closed episodes with this tag (else review in cockpit)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    cfg = GexConfig()
    engine = get_engine(_resolve_db_url(args.db))
    with Session(engine) as session:
        if args.cmd == "plan":
            for p in run_plan(session, cfg=cfg):
                log.info("%s: bias=%s regime=%s -> %s", p.underlying, p.bias, p.regime, p.call)
        elif args.cmd == "settle":
            res = run_settle(session, cfg=cfg)
            log.info("settled=%d skipped_no_bars=%d", res.settled, res.skipped_no_bars)
        elif args.cmd == "analyze":
            levels, liq = run_analyze(args.ticker, cfg=cfg, save=args.save, session=session)
            _print_levels(args.ticker, levels, liq)
        elif args.cmd == "import-robinhood":
            print(run_import(session, args.csv, tag_all=args.tag_all))


if __name__ == "__main__":
    main()
