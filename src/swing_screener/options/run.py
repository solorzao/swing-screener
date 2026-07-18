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
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.data.fetch import fetch_bars
from swing_screener.db.models import GexSnapshot, OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options import broker_import
from swing_screener.options.autograde import AutoGrade, autograde
from swing_screener.options.chain import (
    ChainSnapshot,
    LiquidityReport,
    _now_eastern,
    assess_liquidity,
    snapshot_chain,
)
from swing_screener.options.config import GexConfig
from swing_screener.options.gex import GexLevels, compute_gex
from swing_screener.options.plan import DayPlan, build_plan, save_snapshot
from swing_screener.options.settle import SettleResult, settle_open_trades
from swing_screener.settings import load_settings

log = logging.getLogger(__name__)

_MARKET_CLOSE_HOUR = 16  # 4pm ET; half-days share settle.py's market-calendar TODO

Snapshotter = Callable[[str, GexConfig], ChainSnapshot]
DailyBars = Callable[[str], pd.DataFrame]
BarsFetcher = Callable[[str], pd.DataFrame]
# The cold-ticker analyze seam for run_autograde. Signature mirrors run_analyze
# (kwargs-only cfg/save/session); the return is ignored -- run_autograde re-reads
# the snapshot it persisted.
Analyzer = Callable[..., tuple[GexLevels, LiquidityReport]]


def _default_snapshotter(ticker: str, cfg: GexConfig) -> ChainSnapshot:
    return snapshot_chain(ticker, cfg=cfg)


def _resolve_cache_dir(cache_dir: Path | None) -> Path:
    """An explicit ``cache_dir`` (the CLI's ``--cache-dir``) wins over settings --
    the flag used to be parsed and silently ignored (2026-07-17 audit, H3)."""
    return cache_dir if cache_dir is not None else load_settings().cache_dir


def _daily_fetcher(cache_dir: Path | None) -> DailyBars:
    cache = _resolve_cache_dir(cache_dir)

    def _fetch(ticker: str) -> pd.DataFrame:
        bars = fetch_bars(ticker, "1d", cache_dir=cache)
        if bars is None or bars.empty:
            raise RuntimeError(f"no daily bars for {ticker}")
        return bars

    return _fetch


def _5m_fetcher(cache_dir: Path | None) -> BarsFetcher:
    # fetch_bars never caches a 5m frame whose session is still in progress
    # (data/fetch.py, 2026-07-17 audit H1 follow-up), so an intraday sweep can't
    # pin a partial session for the post-close run; complete sessions cache per
    # day as before (the completed-bar invariant, docs/modules/gex-lab.md).
    cache = _resolve_cache_dir(cache_dir)

    def _fetch(ticker: str) -> pd.DataFrame:
        bars = fetch_bars(ticker, "5m", cache_dir=cache, period="5d")
        if bars is None or bars.empty:
            raise RuntimeError(f"no 5m bars for {ticker}")
        return bars

    return _fetch


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
    cache_dir: Path | None = None,
) -> list[DayPlan]:
    """Build and persist a morning GEX map + day plan for each watchlist ticker.

    Per-ticker failure isolation: one bad chain never sinks the rest of the plan.
    ``cache_dir`` only steers the default daily-bar fetcher; an injected
    ``daily_bars`` brings its own caching (or none).
    """
    snap_fn = snapshotter or _default_snapshotter
    daily_fn = daily_bars or _daily_fetcher(cache_dir)
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


def _latest_snapshot(session: Session, underlying: str) -> GexSnapshot | None:
    """The newest GEX snapshot for an underlying (any day) -- the same latest-row
    query the plan router serves. Autograde's same-day gate handles staleness, so
    this deliberately does not filter by date."""
    return session.scalars(
        select(GexSnapshot).where(GexSnapshot.underlying == underlying)
        .order_by(GexSnapshot.ts.desc()).limit(1)
    ).first()


def _try_bars(fetch: BarsFetcher, ticker: str) -> pd.DataFrame | None:
    """Fetch bars for autograde, degrading any failure to None so the affected item
    grades 'unavailable' rather than sinking the whole read (run_plan's per-ticker
    isolation posture)."""
    try:
        return fetch(ticker)
    except Exception:
        log.exception("autograde: bar fetch failed for %s; its items degrade", ticker)
        return None


def run_autograde(
    underlying: str, direction: str, play_type: str,
    entry: float | None, stop: float | None, target: float | None,
    pivot_level: float | None, *,
    cfg: GexConfig, session: Session,
    daily_fetcher: DailyBars | None = None,
    m5_fetcher: BarsFetcher | None = None,
    analyzer: Analyzer | None = None,
    now: Callable[[], datetime] | None = None,
) -> AutoGrade:
    """Wire the pure ``autograde`` to the lab's data seams: fetch daily + 5m bars,
    load (or auto-produce) a same-day GEX snapshot, and machine-grade the eight
    computable checklist items for one ticker.

    Isolation posture (run_plan's): a bar-fetch failure or a failed cold-ticker
    auto-analyze degrades the affected items to ``unavailable`` -- nothing here
    raises except the direction ``ValueError`` (``autograde``'s contract; the
    router turns it into a 422). A dead DB is the one genuinely unexpected failure
    and rides the app-level handler as usual.

    ``daily_fetcher`` / ``m5_fetcher`` default to the real cached fetchers
    (``_daily_fetcher`` / ``_5m_fetcher``, reused from the plan/settle paths);
    ``analyzer`` to ``run_analyze`` (the one-click cold-ticker fallback);
    ``now`` to ``_now_eastern`` (naive US/Eastern, injectable for tests)."""
    if direction not in ("long", "short"):
        # Fail before any fetch; the router validates first, this is the seam-level
        # backstop that keeps a bad direction from silently grading as short.
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    daily_fn = daily_fetcher or _daily_fetcher(None)
    m5_fn = m5_fetcher or _5m_fetcher(None)
    analyze_fn = analyzer or run_analyze
    today = (now or _now_eastern)().date()

    daily_bars = _try_bars(daily_fn, underlying)
    bars_5m = _try_bars(m5_fn, underlying)

    snapshot = _latest_snapshot(session, underlying)
    if snapshot is None or snapshot.ts.date() != today:
        # Cold (or stale) ticker: produce today's snapshot in one click. A dead
        # upstream degrades the snapshot-backed items to 'unavailable' -- the honest
        # 'incomplete', never a 503 for the whole read.
        try:
            analyze_fn(underlying, cfg=cfg, save=True, session=session)
        except Exception:
            log.exception(
                "autograde: auto-analyze failed for %s; snapshot items degrade",
                underlying)
        else:
            snapshot = _latest_snapshot(session, underlying)

    return autograde(
        underlying, direction, play_type, entry, stop, target, pivot_level,
        cfg=cfg, daily_bars=daily_bars, bars_5m=bars_5m, snapshot=snapshot, now=now,
    )


def run_settle(
    session: Session, *, cfg: GexConfig, bars_fetcher: BarsFetcher | None = None,
    cache_dir: Path | None = None,
) -> SettleResult:
    """Settle every open options-lab trade against its underlying's completed 5m bars.

    Untouched trades whose frame lacks a complete session stay open (see
    ``settle_open_trades`` -- the eod_flat fallback is gated on session
    completeness, so an intraday sweep can never flatten at a mid-session price).
    ``cache_dir`` only steers the default 5m fetcher; an injected ``bars_fetcher``
    brings its own caching (or none)."""
    fetch = bars_fetcher or _5m_fetcher(cache_dir)
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
    p_st = sub.add_parser("settle", help="settle open lab trades from completed 5m bars")
    p_st.add_argument("--force", action="store_true",
                      help="run before the 16:00 ET close (intraday stop/target exits "
                           "settle; untouched trades stay open on incomplete sessions)")
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
            for p in run_plan(session, cfg=cfg, cache_dir=args.cache_dir):
                log.info("%s: bias=%s regime=%s -> %s", p.underlying, p.bias, p.regime, p.call)
        elif args.cmd == "settle":
            now = _now_eastern()
            if now.hour < _MARKET_CLOSE_HOUR and not args.force:
                if now.weekday() >= 5:  # Sat/Sun: the intraday rationale would mislead
                    raise SystemExit(
                        f"refusing to settle at {now:%a %H:%M} ET: weekend run before "
                        "16:00 -- no session is in progress, the last session is "
                        "already complete; pass --force to run now or wait until "
                        "after 16:00"
                    )
                raise SystemExit(
                    f"refusing to settle at {now:%H:%M} ET, before the 16:00 close: "
                    "an intraday run pins the day-keyed 5m cache on a partial session; "
                    "pass --force to sweep intraday stop/target exits anyway "
                    "(untouched trades stay open either way)"
                )
            res = run_settle(session, cfg=cfg, cache_dir=args.cache_dir)
            log.info("settled=%d skipped_no_bars=%d skipped_incomplete_session=%d",
                     res.settled, res.skipped_no_bars, res.skipped_incomplete_session)
        elif args.cmd == "analyze":
            levels, liq = run_analyze(args.ticker, cfg=cfg, save=args.save, session=session)
            _print_levels(args.ticker, levels, liq)
        elif args.cmd == "import-robinhood":
            print(run_import(session, args.csv, tag_all=args.tag_all))


if __name__ == "__main__":
    main()
