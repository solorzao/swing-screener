# Running the screener locally (Phase 2)

The Phase 2 pipeline runs the engine over the universe on your machine against a
local SQLite database — no Azure yet. This is the **dry-run** setup: run it after
the close for a couple of weeks so the shadow book accumulates real forward-test
results before building the dashboard (Phase 3).

## Prerequisites

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

## Run it

Full universe (503 S&P 500 names; cold run takes a few minutes as it fetches and
caches each ticker, then is fast on subsequent same-day runs):

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.run --db sqlite:///local.db --cache-dir .cache --chart-dir .charts
```

Or the convenience wrapper (same defaults):

```powershell
.\.venv\Scripts\python scripts\run_local.py
```

Sanity-check on a small slice first:

```powershell
.\.venv\Scripts\python scripts\run_local.py --max-tickers 50
```

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `sqlite:///local.db` | SQLAlchemy DB URL — matches the dashboard's default so both agree (same code targets Azure SQL later) |
| `--universe` | `src/swing_screener/data/universe_seed.csv` | ticker list to scan |
| `--cache-dir` | `.cache` | parquet bar cache (keyed per ticker/interval/day) |
| `--chart-dir` | `.charts` | annotated HA chart PNGs for the top picks |
| `--top-charts` | `5` | how many top-ranked signals get a chart |
| `--max-tickers` | (all) | cap the universe for a quick run |

## What a run does

1. Loads the universe.
2. Per ticker (isolated — one bad ticker never aborts the run): fetches 1h→4h, 1d,
   1wk, 1mo bars (cached as parquet) and builds the enriched frames.
3. Detects the pullback-continuation signal on each timeframe, scores + ranks them,
   and writes `signals` rows (rank 1 = highest score) for the run date.
4. Renders annotated Heiken Ashi charts for the top N into `--chart-dir`.
5. **Shadow book:** fills the *previous* bar's signals against the latest bar
   (worst-case in-zone), advances open paper trades through the tiered exits, and
   records outcomes — the self-grading forward test.

Re-running the same day is idempotent: it clears that day's signals/paper trades
first, so a retry won't double-count.

## Inspecting results

```powershell
.\.venv\Scripts\python -c "from sqlalchemy.orm import Session; from swing_screener.db.session import get_engine; from swing_screener.db import repo; from datetime import date; s=Session(get_engine('sqlite:///local.db')); rows=repo.latest_signals(s, date.today()); print(len(rows), 'signals'); [print(r.rank, r.ticker, r.timeframe, round(r.score,3), r.quality_tier, r.volatility_tier) for r in rows[:10]]"
```

Charts land in `.charts/<TICKER>_<TF>_<YYYYMMDD>.png`. Both the DB and these dirs
are gitignored.

## Next

After a 1–2 week dry-run validates signal quality, proceed to Phase 3 (local
Streamlit dashboard) and Phase 4 (email + LLM analysis).
