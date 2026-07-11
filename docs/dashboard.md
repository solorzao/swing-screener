# Local dashboard (Phase 3)

> **Superseded by the desktop cockpit.** New work happens in the cockpit
> ([docs/cockpit.md](cockpit.md); design:
> [docs/plans/2026-07-05-desktop-ui-design.md](plans/2026-07-05-desktop-ui-design.md)).
> These Streamlit pages retire as native equivalents land, per the design doc's
> migration plan; until then this dashboard keeps running untouched.

A private **Streamlit** dashboard over the same SQLite store the pipeline writes — browse
the day's candidates, log and track real trades with live unrealized P/L, and review the
analyst's calibration. It runs **locally only** (no public access).

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

A **left sidebar radio** switches between ten pages (grouped here for orientation; the
sidebar lists them in this order). **Overview** is the default landing page.

> The **Screener Performance** and **System Health** pages retired to the cockpit
> ([docs/cockpit.md](cockpit.md)): performance stats live at `/api/stats/performance`
> (the Performance panel); the autonomy gate, execution mode, and analyst spend at
> `/api/gate` (the gate chip); and run freshness on the heartbeat rail
> (`/api/heartbeats`).

**Overview** — a KPI dashboard: open positions, total unrealized P/L, today's candidate
count, and the screener's win rate, plus a **recent activity** feed of the latest exit
events. The one-glance "where do I stand" page.

**Signals**

- **Today's Candidates** — the day's ranked signals with a **play-type filter**
  (All / Continuation / Reversal). The table surfaces rank, play type, strength, timeframe,
  horizon, score, RSI, ATR, MTF alignment, oversold flag, quality/volatility tiers, entry
  zone, stop, and target; the annotated chart for each row lives in a **per-row expander**.
- **Deep Analysis** — request an on-demand, in-depth **multi-timeframe** read of any ticker.
  The dashboard queues the request (to Azure SQL); a scheduled cloud worker runs the full deep
  analysis (Opus across 4h/1d/1wk/1mo, web-searched), **emails a PDF** report, and the result
  shows here — status badges (🕓 queued · ⏳ running · ✅ done · ⚠️ failed), the summary, the
  per-timeframe charts, and a PDF download.

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

- **Analyst Calibration** — the LLM analyst's scored conviction calls per play type: is
  its judgment earning R? A table of scored-call counts and mean R by conviction grade,
  plus the mean R of its **nudges** (calls where the final conviction differs from the
  baseline).
- **Exit Log** — exit events recorded by the nightly run, **filterable** by reason and by
  paper/real book.

**Reference**

- **Universe** — a **searchable** list of the screening universe (ticker, name, exchange,
  market cap, average dollar volume). Populated by the pipeline: each screen run syncs the
  seed and records best-effort market cap + average dollar volume per ticker (a metric may
  be blank if its fetch failed that run).
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
