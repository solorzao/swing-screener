# Swing Screener — Design

**Date:** 2026-06-14
**Status:** Validated design, ready for implementation planning

## Purpose

Automate the discovery, categorization, tracking, and exit-monitoring of swing trades
that match a Heiken Ashi pullback-continuation strategy, across four timeframes, with
daily/weekly/monthly email digests and a private local dashboard. Includes a self-grading
"shadow book" to measure and improve screener quality over time.

Not in scope: day-trading. Options are deferred to a later phase (needs a paid data feed).

## The strategy (v1 signal spec)

A **trend-continuation pullback long entry**, evaluated independently on 4h / Daily /
Weekly / Monthly bars. The timeframe of the signal sets the expected hold length:

- 4h → short (1–3 days)
- Daily → medium (days to a week)
- Weekly → weeks
- Monthly → months

Heiken Ashi terms map to precise math:

- **Bullish shaved bottom** = green HA candle with no lower wick (`HA_Low == HA_Open`) → strong up-momentum
- **Bearish shaved head** = red HA candle with no upper wick (`HA_High == HA_Open`) → momentum rolling over
- **Zone/doji** = small body relative to range (pullback exhausting)

HA transform:
```
HA_Close = (O + H + L + C) / 4
HA_Open  = (prev HA_Open + prev HA_Close) / 2
HA_High  = max(H, HA_Open, HA_Close)
HA_Low   = min(L, HA_Open, HA_Close)
```

**Long signal**, evaluated on the most recently *closed* bar of each timeframe:

1. **Uptrend context** — `EMA20 > EMA50` and close above `EMA50` ("market was rising").
2. **Shallow pullback** — a prior bearish shaved head, then 1–4 red/zone bars that hold
   above `EMA50` / the prior swing low (continuation, not reversal).
3. **Trigger** — the current bar is the first bullish HA candle out of the zone, bonus if
   it's a bullish shaved bottom with a strong body.

Thresholds (EMA lengths, max pullback depth, min green-body size) are **config**, tuned
against live output and the golden test, not architecture.

### Entry zone (range, not a single price)

The trigger fires on a *closed* bar, so entries happen on the next bar — price has already
moved. Every signal therefore carries an ATR-scaled **entry zone** (auto-scales across
4h/D/W/M):

- **Floor** = pullback support (EMA50 / prior swing low) + a small buffer.
- **Ceiling** = trigger bar's close + ~0.25–0.5×ATR buffer.

You enter anywhere in `[floor, ceiling]` and do **not chase** above the ceiling. The stop
sits just below the floor; target is ATR-multiple / prior swing high measured from the zone.

**Fill rules:**
- Next bar trades within `[floor, ceiling]` → **filled**.
  - **Shadow book uses the worst-case in-zone price** for a long: the highest price the bar
    traded that is still inside the zone, `min(bar_high, ceiling)`. This deliberately gives
    the smallest reward / widest risk so the shadow book can only *understate* the screener,
    never flatter it.
  - Real-trade *suggestions* show the full zone range — you choose your own fill.
- Next bar gaps/runs entirely **above the ceiling** → **missed entry** (no fill, recorded).
- Next bar gaps **below the stop** → **invalidated** (no fill, recorded).
- This yields a true **fill rate** metric — how many signals were actually enterable.

### Multi-timeframe handling: "flag all, boost aligned"

Screen every timeframe independently; surface any valid signal tagged with its horizon.
Rank a signal **higher** when a higher timeframe also confirms an uptrend (a Daily trigger
scores higher if the Weekly is also bullish). MTF alignment is a key ranking input.

### Exit logic: tiered, weighted, with a hard override

The engine computes a composite exit score, but stop-loss is an absolute override.

- 🔴 **Hard stop** — stop-loss breach = unconditional SELL, overrides everything, no exceptions.
- 🟠 **Strong** — Heiken Ashi momentum flip (bearish shaved head on the trade's timeframe).
- 🟡 **Advisory** — price target reached, or time-stop exceeded for the horizon.

Real trades generate **alerts to the user**. Paper trades just **close and record outcome**.

## Architecture

Three runtimes around one shared store. The shared store is the contract between the
Azure job and the local dashboard — they never talk directly.

```
                    ┌─────────────────────────────────────────┐
                    │         AZURE (private, no public)        │
                    │                                           │
  Scheduled  ─────► │  Screener Job (Container Apps Job)         │
  (cron: post-close,│   1. Fetch OHLCV (yfinance, ~1-2k tickers)│
   pre-open, 4h,    │   2. Compute Heiken Ashi, all timeframes   │
   weekly, monthly) │   3. Detect pullback-continuation signal   │
                    │   4. Categorize + rank                     │
                    │   5. Render annotated HA charts (mplfinance)│
                    │   6. LLM analysis (Claude API) → rationale │
                    │   7. Shadow book + check exits             │
                    │   8. Send emails (Gmail SMTP)              │
                    │            │                │              │
                    │            ▼                ▼              │
                    │   ┌──────────────┐  ┌──────────────────┐  │
                    │   │ Azure SQL    │  │  Blob Storage     │  │
                    │   │ (serverless) │  │  (charts, bars)   │  │
                    │   └──────┬───────┘  └────────┬─────────┘  │
                    │      Key Vault (secrets)      │            │
                    └──────────┼───────────────────┼────────────┘
                               │ (authenticated)   │
                    ┌──────────▼───────────────────▼────────────┐
                    │   LOCAL PC                                  │
                    │   Streamlit dashboard (127.0.0.1 only)      │
                    │   - candidates, active trades, P/L          │
                    │   - manual trade entry → writes to store    │
                    │   - charts, exit alerts, screener perf      │
                    └─────────────────────────────────────────────┘
```

**Key decisions:**
- **No heavy agent framework** (no n8n/LangGraph for v1). A linear nightly pipeline. The
  only "agent" is a Claude API call that narrates the shortlist.
- **Deterministic screening, LLM narration.** Pattern detection is math (fast, cheap,
  reliable); the LLM only writes rationale over the deterministic facts and cannot
  invent a setup that the engine didn't find.

## Foundational choices

| Decision | Choice |
|---|---|
| Market data | Free (yfinance / Stooq fallback) |
| Universe | Liquid large/mid caps (~1–2k), refreshed weekly |
| Trade tracking | Manual entry via dashboard |
| Language/stack | Python |
| Dashboard | Local Streamlit, bound to `127.0.0.1` only |
| Job hosting | Azure (Container Apps Jobs), code is cloud-target from day one |
| Email | Gmail SMTP (app password) |
| LLM | Claude Sonnet 4.6 (upgradeable to Opus) |
| Options | Deferred to phase 2 (needs paid feed) |

## Screening engine

1. **Data fetch** — per ticker, pull 1h (→ resample to 4h) and 1d (→ resample weekly/monthly).
   Cache per run as Parquet in Blob so re-runs don't re-hit Yahoo.
2. **Heiken Ashi** — transform per timeframe; derive `bullish`, `bearish`, `shaved_bottom`,
   `shaved_head`, `doji/zone` booleans per bar.
3. **Signal detection** — the v1 spec above, on the most recently closed bar.
4. **Metrics & categorization** per hit:
   - **Horizon** — from timeframe.
   - **Quality tier** — market cap + price + avg dollar volume → reputable / mid / speculative / penny.
   - **Volatility** — ATR% + realized vol → low/med/high.
   - **Oversold flag** — RSI(14) + distance below mean (mean-reversion tag).
   - **MTF alignment** — higher timeframes also in uptrend/signal?
5. **Ranking** — composite score over signal strength, MTF alignment, trend slope,
   liquidity/quality, volatility-fit. Top 5 (daily) feed the email; **all** hits feed the shadow book.

## Analysis, charts & email

- **Charts** — `mplfinance` renders annotated HA charts (HA candles, EMA20/50, shaded
  pullback zone, marked trigger bar, shaded **entry zone** band, stop/target lines). PNGs to
  private Blob. For MTF-aligned plays, render the trade timeframe plus the next one up.
- **LLM analysis** — only the shortlist hits Claude. Inputs are the computed facts; output
  is a tight rationale (why it qualifies, conviction, entry/stop/target reasoning, horizon).
  The model narrates deterministic findings — it does not invent signals.
- **Emails:**
  - **Daily** (pre-open): top 5 stock picks, each with rationale + inline (CID) chart, plus
    any exit alerts on active trades.
  - **Weekly** (one day/week): top Weekly-timeframe plays.
  - **Monthly** (once/month): top Monthly-timeframe plays.
  - **Exit alerts** ride along with the detecting run; hard stops can fire their own urgent email.

## Storage & data model

**Azure SQL Database (serverless)** — auto-pauses to zero compute when idle; pay mostly for
storage. Accessed via **SQLAlchemy** (same code runs on local SQLite in dev). Chart PNGs and
Parquet bar-cache in **private Blob containers**. Secrets in **Azure Key Vault**; the job uses
a managed identity. The local dashboard authenticates with the user's `az login` credentials.

Tables:
- `universe` — tickers + name, exchange, market cap, avg dollar volume (refreshed weekly).
- `signals` — every detected signal per run: ticker, timeframe, run date, metrics, composite
  score, that-night rank, **entry zone (floor/ceiling), stop, target**, chart blob paths.
- `trades` — **real trades** (manual): ticker, horizon, entry date/price, size, stop, target,
  status, exit date/price/reason, notes, linked `signal_id`. P/L computed live from latest bars.
- `paper_trades` — **shadow book**: identical shape + QC fields (signal score, rank,
  MTF-alignment, **fill status** [filled / missed / invalidated], realized R, hold bars,
  exit reason). Auto-opened from every valid signal; **filled only if the next bar trades
  within the entry zone** (else recorded as missed/invalidated), risk-normalized to 1R.
- `exit_events` — every exit alert fired (trade/paper, type, strength tier, date, message).
- `email_log` — what was sent when (idempotency / audit).

**Exit monitoring (each run):** load open `trades` + `paper_trades`, pull latest bars,
evaluate tiered exit. Hard-stop breach → close + 🔴 immediately; momentum flip → 🟠;
target/time-stop → 🟡.

## Shadow book (screener quality control)

Auto-paper-trade **every valid signal** (not just the emailed top 5), each tagged with its
rank/score that night. Realistic fills: the next bar must trade **within the entry zone** to
fill, **at the worst-case in-zone price** (`min(bar_high, ceiling)` for a long); gaps above
the ceiling → *missed*; gaps below the stop → *invalidated*; auto-derived
stop (pullback swing low / EMA50) and target (ATR-multiple / prior swing high); position
**risk-normalized to 1R** so all trades are comparable. Filled trades are tracked through the
same exit logic until closed, then scored. Aggregates (win rate, expectancy in R, profit
factor, avg hold, and **fill rate**) are sliced by timeframe, quality tier, volatility,
MTF-alignment, and **rank bucket** — answering both "does the ranking actually pick winners?"
and "how often could I actually enter?" Stored separately from real trades.

## Dashboard tabs (local Streamlit)

1. **Today's Candidates** — ranked signals; filters for timeframe/horizon/quality/volatility;
   chart + Claude rationale + suggested **entry zone** / stop / target + MTF badge. "Take this
   trade" pre-fills the entry form (with the zone).
2. **Active Trades** — open positions: live price, unrealized P/L ($ and %), size, entry,
   stop, target, distance-to-target/stop, live exit-alert badge, expandable chart.
3. **Trade Entry / Management** — manual form; edit/close trades.
4. **Closed Trades** — realized history, per-trade + cumulative P/L, hold time, exit reason.
5. **Screener Performance** — shadow-book aggregates; headline chart of win rate / avg-R by
   rank bucket; **fill rate** (filled vs missed vs invalidated); paper-book equity curve.
6. **Exit Log** — every alert fired, when, and outcome.

Localhost-only, no auth needed. Charts pulled from Blob.

## Scheduling, resilience, testing

**Scheduling (Azure Container Apps Jobs, cron in ET, DST-aware):**
- **Evening (post-close ~4:15pm ET):** full screen → signals, shadow book, charts, analysis, stored.
- **Pre-open (~8:00am ET):** exit re-check (overnight gaps) + send daily top-5 email.
- **Intraday 4h:** lightweight exit-check after each 4h bar close (short-horizon trades). Configurable.
- **Weekly** (after Fri close) and **Monthly** (after last session of month) emails.

**Error handling / resilience:**
- yfinance is unofficial and rate-limits cloud IPs: **per-ticker isolation**, retry-with-backoff,
  optional Stooq fallback, last-good-bar cache.
- **Graceful degradation:** LLM down → email sends with deterministic facts; charts fail →
  text-only email. Never block the alert.
- **Idempotency:** runs keyed by date; `email_log` prevents double-sends.
- **Health heartbeat:** if the evening run is missing, the pre-open job emails a warning.
  Logs to Application Insights.

**Testing:**
- Unit tests on the deterministic core (HA transform, signal, exits, ranking, 1R normalization),
  with the **AMD chart as a golden test** — the engine must flag that known entry.
- Historical backtest harness to validate rules before go-live (shadow book then forward-tests).
- End-to-end integration test on a tiny universe with mocked email + temp SQLite.
- Engine built test-first.

## CI/CD

- **Repo:** private GitHub repo (`solorzao/swing-screener`).
- **CI (from Phase 1):** GitHub Actions runs `ruff` + `mypy` + `pytest` (incl. the golden AMD
  test) on every push and PR. This is the gate that protects signal correctness while the
  strategy config is tuned.
- **CD (Phase 5):** on merge to `main`, with CI green, a workflow builds the job container,
  pushes it to Azure Container Registry, and updates the Container Apps Job. Deploys are
  gated on passing tests. Secrets via GitHub Actions secrets / OIDC to Azure; no long-lived
  credentials in the repo.

## Build roadmap

Azure remains the deployment target throughout; the engine is written/tested before the
cloud cron is flipped on.

1. **Engine core** — HA + signal + exit + ranking, tested, validated vs. the user's charts.
2. **Pipeline + shadow book + charts** — universe fetch, nightly pipeline → SQLite, mplfinance.
3. **Dashboard** — Streamlit tabs over the local DB.
4. **Email + LLM analysis** — Gmail SMTP, Claude rationale, three cadences + exit alerts.
5. **Deploy to Azure** — Container Apps Jobs, SQL serverless, Blob, Key Vault.
6. **Options module** — phase 2, when a paid feed is added.

> Pragmatic note: although hosting is "Azure from day one," a few days of **local dry-run**
> after step 2 is recommended to validate signal quality before paying Azure to discover the
> rules need tuning. The code is cloud-target regardless; only the cron flip is deferred.
