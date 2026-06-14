# Local dashboard (Phase 3)

A private **Streamlit** dashboard over the same SQLite store the pipeline writes — browse
the day's candidates, log and track real trades with live unrealized P/L, and review the
shadow book's screener-performance stats. It runs **locally only** (no public access).

## Run it

```powershell
# point it at the DB your pipeline writes (see docs/running-locally.md)
$env:SWING_DB_URL = "sqlite:///local.db"
$env:SWING_CACHE_DIR = ".cache"   # reused for live-quote lookups

.\.venv\Scripts\python -m streamlit run src\swing_screener\dashboard\app.py --server.address 127.0.0.1
```

`--server.address 127.0.0.1` binds to localhost only. Streamlit opens it in your browser at
`http://127.0.0.1:8501`. Leave it running; it re-reads the DB on each interaction.

### Config (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `SWING_DB_URL` | `sqlite:///local.db` | the SQLite DB the pipeline writes (same code targets Azure SQL later) |
| `SWING_CACHE_DIR` | `.cache` | parquet cache reused to fetch the latest close for live P/L |

## The tabs

1. **Today's Candidates** — the day's ranked signals (ticker, timeframe, horizon, score, MTF,
   quality/volatility tags, entry zone, stop, target) with the annotated chart when available.
2. **Active Trades** — your open trades with **live unrealized P/L** ($ and %), R-multiple,
   distance to stop/target, and a quick badge (🔴 at/through stop, 🟡 at/through target, 🟢 holding).
3. **Trade Entry** — log a new real trade (ticker, timeframe, entry, size, stop, target, notes).
4. **Closed Trades** — realized history with per-trade and cumulative P/L.
5. **Screener Performance** — the shadow book's QC: fill rate, win rate, expectancy (R), profit
   factor; win-rate breakdowns by timeframe and **rank bucket**; the equity curve. This is how
   you tell whether the screener — and your ranking — actually works.
6. **Exit Log** — exit events recorded by the nightly run.

## Notes

- The dashboard is read-mostly; it never places trades. Trade entry just records what you did.
- The **Claude-written rationale** on candidates arrives in Phase 4 (email + LLM). Phase 3 shows
  the deterministic metrics + charts.
- Live quotes use the cached daily close (delayed, free yfinance); a ticker with no quote shows "—".
