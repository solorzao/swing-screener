"""D1: where does the continuation trigger actually FIRE within its own setup?

Not a lever sweep. Q6 (entry price above market) and Q8 (below it) both graded NULL, so
the entry-price axis is closed in both directions and the edge file's standing note is
that any future proposal must be a different SETUP or exit. This audit asks whether the
setup is specified the way we think it is.

The lead: Q8 showed that bidding 0.50 ATR BELOW the flip-bar close recovers +0.146R. That
says the shipped entry sits systematically too high above the setup's own low. Heiken-Ashi
is smoothed, so a bullish flip cannot coincide with the true pullback low -- it must lag
it -- and that lag has never been measured. If it is large, the defect is the trigger's
timing within the setup (a designable fix: a non-HA trigger nearer the low). If it is
small, the thesis is structurally dead.

Recomputes per-trigger geometry by walking each cached frame and calling the REAL
``detect_last_bar`` on the truncated history (never a re-implementation, so the geometry
describes the shipped detector), then joins outcomes from the existing D_dump book.

Read-only over the cache. See docs/plans/2026-08-16-trigger-geometry-diagnostic.md --
section C is DESCRIPTIVE ONLY and cannot certify anything (the rank sweep already
falsified geometry-as-selector).

    PYTHONPATH=src:scripts python scripts/diag_trigger_geometry.py --cache-dir .cache \
        --as-of 20260703 --out docs/plans/2026-08-16-trigger-geometry-results.md
"""

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from _replay_common import load_replay_corpus, unique_tickers
from swing_screener.config import StrategyConfig
from swing_screener.signals.detect import detect_last_bar
from swing_screener.signals.entry_zone import compute_zone
from swing_screener.signals.frame import build_frame

log = logging.getLogger(__name__)

_MD: list[str] = []


def _emit(line: str = "") -> None:
    print(line, flush=True)  # noqa: T201
    _MD.append(line)


def _leg_origin(f: pd.DataFrame, t: int) -> int | None:
    """Index of the bar where the current up-leg began: the most recent bar at/before ``t``
    where ema_fast was NOT above ema_slow. None when the leg predates the history
    (left-censored -- excluded from leg maturity rather than silently clamped to bar 0)."""
    fast = f["ema_fast"].to_numpy()
    slow = f["ema_slow"].to_numpy()
    below = np.where(fast[: t + 1] <= slow[: t + 1])[0]
    return int(below[-1]) if len(below) else None


def _geometry_for(ticker: str, f: pd.DataFrame, cfg: StrategyConfig) -> list[dict]:
    """Walk the frame; on every bar where the SHIPPED detector fires, record geometry."""
    out: list[dict] = []
    lows = f["low"].to_numpy()
    closes = f["close"].to_numpy()
    highs = f["high"].to_numpy()
    n = len(f)
    # Warm-up: the detector itself needs ema_slow + max_pullback_bars + 2 bars.
    start = cfg.ema_slow + cfg.max_pullback_bars + 2
    for t in range(start, n):
        sub = f.iloc[: t + 1]
        ctx = detect_last_bar(sub, cfg)
        if ctx is None:
            continue
        if cfg.max_extension_atr > 0 and ctx.extension_atr > cfg.max_extension_atr:
            continue  # the shipped freshness gate -- describe the book we actually trade
        zone = compute_zone(ctx.trigger_close, ctx.atr, ctx.swing_low, cfg,
                            recent_highs=highs[max(0, t - 60):t + 1].tolist())
        if zone is None:
            continue
        atr = ctx.atr
        if not np.isfinite(atr) or atr <= 0:
            continue
        # The swing-low BAR inside the pullback window (ctx carries the price, not the bar).
        k = max(1, ctx.pullback_bars)
        lo_slice = lows[max(0, t - k):t]          # the pullback bars, trigger excluded
        bars_since_low = int(len(lo_slice) - int(np.argmin(lo_slice))) if len(lo_slice) else 0

        origin = _leg_origin(f, t)
        leg_bars = None if origin is None else t - origin
        leg_ext = None if origin is None else (ctx.trigger_close - closes[origin]) / atr
        retrace = None
        if origin is not None and t - k > origin:
            leg_high = float(np.max(highs[origin:t - k + 1]))
            denom = leg_high - closes[origin]
            if denom > 0:
                retrace = (leg_high - ctx.swing_low) / denom

        out.append({
            "ticker": ticker,
            "trigger_date": f.index[t],
            # entry_date convention: the shadow book evaluates the fill on the NEXT bar.
            "entry_date": f.index[t + 1] if t + 1 < n else pd.NaT,
            "bars_since_low": bars_since_low,
            "pullback_bars": ctx.pullback_bars,
            "entry_above_low_atr": (ctx.trigger_close - ctx.swing_low) / atr,
            "ceiling_above_low_atr": (zone.ceiling - ctx.swing_low) / atr,
            "stop_distance_atr": (zone.ceiling - zone.stop) / atr,
            "extension_atr": ctx.extension_atr,
            "leg_bars": leg_bars,
            "leg_extension_atr": leg_ext,
            "retrace_frac": retrace,
        })
    return out


def _pct_table(s: pd.Series, label: str) -> None:
    s = s.dropna().astype(float)
    if s.empty:
        _emit(f"\n**{label}** — no measurable rows.")
        return
    qs = [0.10, 0.25, 0.50, 0.75, 0.90]
    vals = " | ".join(f"{s.quantile(q):.2f}" for q in qs)
    _emit(f"| {label} | {len(s)} | {s.mean():.2f} | {vals} |")


def _outcome_by_decile(df: pd.DataFrame, col: str) -> None:
    """Section C: DESCRIPTIVE ONLY (see the pre-registration)."""
    d = df[df[col].notna() & df.realized_r.notna()].copy()
    if len(d) < 100:
        _emit(f"\n**{col}** — too few joined rows ({len(d)}) to bucket.")
        return
    try:
        d["bucket"] = pd.qcut(d[col].astype(float), 5, duplicates="drop")
    except ValueError:
        _emit(f"\n**{col}** — not enough distinct values to bucket.")
        return
    _emit(f"\n**{col}** (quintiles, filled+closed default book)")
    _emit("\n| bucket | n | tickers | mean R |")
    _emit("|---|---|---|---|")
    for b, g in d.groupby("bucket", observed=True):
        _emit(f"| {b} | {len(g)} | {g.ticker.nunique()} | {g.realized_r.mean():+.3f} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    ap.add_argument("--as-of", default="20260703")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path,
                    default=Path("docs/plans/2026-08-16-trigger-geometry-results.md"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = StrategyConfig()
    tickers = unique_tickers(args.cache_dir)
    if args.limit:
        tickers = tickers[: args.limit]
    frames = load_replay_corpus(args.cache_dir, tickers=tickers, as_of=args.as_of)
    log.info("geometry walk: %d tickers", len(frames))

    rows: list[dict] = []
    for i, (tk, raw) in enumerate(frames.items(), 1):
        rows.extend(_geometry_for(tk, build_frame(raw, cfg), cfg))
        if i % 50 == 0:
            log.info("  %d/%d tickers, %d triggers", i, len(frames), len(rows))
    geo = pd.DataFrame(rows)

    _emit("# D1 — continuation trigger-geometry specification audit")
    _emit(f"\nPinned corpus as-of {args.as_of}; shipped `StrategyConfig` (freshness gate "
          "ON, so this describes the book actually traded). Geometry comes from the REAL "
          "`detect_last_bar`, never a re-implementation. Pre-registration: "
          "docs/plans/2026-08-16-trigger-geometry-diagnostic.md")
    _emit(f"\n- tickers walked: {len(frames)}")
    _emit(f"- triggers recorded: {len(geo)}")
    if geo.empty:
        args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
        return

    _emit("\n## A — where we buy, relative to the setup's own low")
    _emit("\nThe HA-lag question. `bars_since_low` counts bars from the pullback's lowest "
          "low to the trigger bar; the ATR columns say how far above that low the trigger "
          "close (and the price actually paid) sits.")
    _emit("\n| metric | n | mean | p10 | p25 | p50 | p75 | p90 |")
    _emit("|---|---|---|---|---|---|---|---|")
    for c in ("bars_since_low", "pullback_bars", "entry_above_low_atr",
              "ceiling_above_low_atr", "stop_distance_atr"):
        _pct_table(geo[c], c)

    _emit("\n## B — leg maturity at trigger")
    _emit("\nIs the trigger firing early in a leg or late in an exhausted one? "
          "`leg_bars`/`leg_extension_atr` measure from the ema_fast/ema_slow cross that "
          "started the leg; left-censored legs are excluded.")
    _emit("\n| metric | n | mean | p10 | p25 | p50 | p75 | p90 |")
    _emit("|---|---|---|---|---|---|---|---|")
    for c in ("leg_bars", "leg_extension_atr", "extension_atr", "retrace_frac"):
        _pct_table(geo[c], c)
    n_cens = int(geo.leg_bars.isna().sum())
    _emit(f"\nLeft-censored legs excluded from B: {n_cens} "
          f"({n_cens / len(geo):.1%} of triggers).")

    # --- join outcomes from the existing D_dump book -------------------------------
    pat = str(args.cache_dir / "queue_experiments" / "D_dump_s*_slip0.05.parquet")
    files = sorted(glob.glob(pat))
    _emit("\n## C — outcome by geometry (DESCRIPTIVE ONLY)")
    if not files:
        _emit(f"\nNo D_dump shards at `{pat}` — section C skipped.")
    else:
        dd = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        book = dd[(dd.play_type == "continuation") & (dd.fill_status == "filled")
                  & (dd.status == "closed") & dd.realized_r.notna()].copy()
        book["entry_date"] = pd.to_datetime(book.entry_date)
        geo["entry_date"] = pd.to_datetime(geo.entry_date)
        m = geo.merge(book[["ticker", "entry_date", "realized_r", "exit_reason"]],
                      on=["ticker", "entry_date"], how="inner")
        _emit(f"\nJoined {len(m)} of {len(book)} filled+closed book rows "
              f"({len(m) / len(book):.1%} match rate) on (ticker, entry_date).")
        _emit("\n> **This section cannot certify anything.** The 2026-07-03 rank sweep "
              "already falsified geometry-as-selector across nine orderings including "
              "freshness and pullback depth. Multiple comparisons are uncontrolled here "
              "by design; apparent winners are noise until separately pre-registered.")
        for c in ("bars_since_low", "entry_above_low_atr", "leg_extension_atr",
                  "extension_atr", "retrace_frac"):
            _outcome_by_decile(m, c)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(_MD) + "\n", encoding="utf-8")
    print(f"\n[results written to {args.out}]", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
