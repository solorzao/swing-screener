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

A small **connection-status chip** in the sidebar shows where you're pointed —
`🟢 Azure SQL · swing` or `🟢 Local SQLite` — without ever printing the raw connection
string. If the DB is unreachable the page renders a friendly "Can't reach the database"
card (with the error tucked behind a "Technical details" expander) instead of a stack
trace, in keeping with the read-mostly, never-crashes design.

### Config (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `SWING_DB_URL` | `sqlite:///local.db` | the SQLite DB the pipeline writes (same code targets Azure SQL later) |
| `SWING_CACHE_DIR` | `.cache` | parquet cache reused to fetch the latest close for live P/L |

## Navigation

A **left sidebar radio** switches between nine pages (grouped here for orientation; the
sidebar lists them in this order). **Overview** is the default landing page.

**Overview** — a KPI dashboard: open positions, total unrealized P/L, today's candidate
count, and the screener's win rate, plus a **recent activity** feed of the latest exit
events. The one-glance "where do I stand" page.

**Signals**

- **Today's Candidates** — the day's ranked signals with a **play-type filter**
  (All / Continuation / Reversal). The table surfaces rank, play type, strength, timeframe,
  horizon, score, RSI, ATR, MTF alignment, oversold flag, quality/volatility tiers, entry
  zone, stop, and target; the annotated chart for each row lives in a **per-row expander**.

**Trades**

- **Active Trades** — your open trades with **live unrealized P/L** ($ and %), R-multiple,
  distance to stop/target, and a quick badge (🔴 at/through stop, 🟡 at/through target,
  🟢 holding). An inline **Close a trade** form closes a selected position (exit date, price,
  reason) right from this page.
- **Trade Entry** — log a new real trade (ticker, timeframe, horizon, entry, size, stop,
  target, notes). It records the trade — it does not place an order.
- **Closed Trades** — realized history as a formatted table with cumulative realized P/L and
  a realized **equity curve** over exit dates.

**Analytics**

- **Screener Performance** — the shadow book's QC as KPI metrics (fill rate, win rate,
  expectancy R, profit factor, count closed) plus charts: win-rate breakdowns by timeframe
  and **rank bucket**, and the cumulative-R equity curve. This is how you tell whether the
  screener — and your ranking — actually works.
- **Exit Log** — exit events recorded by the nightly run, **filterable** by reason and by
  paper/real book.

**Reference**

- **Universe** — a **searchable** list of the screening universe (ticker, name, exchange,
  market cap, average dollar volume).
- **Digest Log** — the history of digest emails the screener has sent (sent time, kind,
  subject, run date).

## Notes

- The dashboard is read-mostly; it never places trades. Trade entry just records what you did.
- The **Claude-written rationale** on candidates arrives in Phase 4 (email + LLM). Phase 3 shows
  the deterministic metrics + charts.
- Live quotes use the cached daily close (delayed, free yfinance); a ticker with no quote shows "—".
- The dashboard **stays local even after the Azure deploy** (Phase 5) — there is no hosted/public
  dashboard. To view the cloud data, `az login` and point `SWING_DB_URL` at Azure SQL
  (`Authentication=ActiveDirectoryDefault`) and `SWING_BLOB_ACCOUNT_URL` at the chart container;
  see the [Azure deploy runbook](azure-deploy.md). It reads candidates from Azure SQL and
  downloads charts from Blob using your Entra identity (no stored credentials). The **ODBC driver
  is auto-detected** — it picks the highest installed `ODBC Driver NN for SQL Server`, so a machine
  with either **ODBC Driver 17 or 18** connects without editing the URL.
