# Swing Screener

Automated screener for **Heiken Ashi swing-trading** strategies. It scans a universe of
stocks after the close, finds setups across multiple timeframes, ranks and categorizes them,
renders annotated charts, emails digests, and forward-tests every signal in a self-grading
**shadow book** so the strategies can be measured and improved with real data. It screens two
complementary **long** plays — trend-**continuation** pullbacks and oversold-bounce
**reversals** — and runs locally or as scheduled Azure Container Apps Jobs.

**▶ [North Star](docs/NORTH_STAR.md) — the project's purpose and goal. Every design and
implementation decision is checked against it.**

> Status: **Phases 1–4 complete** (engine, pipeline + shadow book, Streamlit dashboard,
> email/LLM digests). **Phase 5 (Azure) is deployed** — six scheduled Container Apps Jobs run
> the screener, digests, exit alerts, and on-demand analysis. See [Roadmap](#roadmap).

---

## The strategies

Two independent **long** screeners run per ticker on **4h / Daily / Weekly / Monthly** bars
(the timeframe sets the expected hold length). Every signal carries a `play_type`, an
ATR-scaled **entry zone** `[floor, ceiling]`, a **stop**, a **target**, categorization tags
(horizon, quality, volatility, oversold), and a composite **score** used for ranking.

### Continuation pullback (the primary play)

1. **Uptrend context** — `EMA20 > EMA50` and price above `EMA50`.
2. **Shallow pullback** — a bearish Heiken Ashi "shaved head" then a few red / small-body
   "zone" candles that hold above `EMA50` (a continuation, not a reversal).
3. **Trigger** — the most recent *closed* bar flips bullish (the first strong green HA candle
   out of the pullback).
4. **Freshness (anti-chase)** — the trigger is rejected if its close already sits more than
   `max_extension_atr` (default **2.0**) ATR above `EMA20`. A setup that has already run far
   off the pullback is a chase, not an entry, so it never reaches the screen *or* the shadow
   book (the freshness rule is part of the strategy end-to-end). Empirically, fresh pullback
   triggers fire under ~1 ATR of extension; late chases fire at ~2.7.

The continuation **target** is structure-aware: the nearest standard-candle swing-high
resistance above the entry ceiling, else an ATR measured-move (`reference + 2·ATR`), floored
at **1.5R** so reward:risk stays sane (this replaced a blind fixed-2R projection that capped
runners).

### Reversal plays (oversold bounce)

A counter-trend complement: a run of **≥3 red HA candles below `EMA50`** that flips green —
tagged `early` (fresh flip) or `confirmed` (flip + follow-through). Entry is a **0.382–0.618
pullback** into the bounce, stop below the bounce origin, target at the **0.786 retrace**
toward the prior breakdown. Surfaced as a "Reversal Plays" list in the daily digest and a
Continuation/Reversal filter throughout the dashboard.

### Scoring & exits

The composite **score** blends HA trigger strength (0.25), multi-timeframe alignment (0.20),
trend slope (0.15), volatility fit (0.10), **RSI** bull-range pullback quality (0.15),
**MACD-histogram** momentum (0.05), and **freshness** (0.10) — the last rewards a trigger that
has *not* already extended away from EMA20, so clean setups outrank chases (0.10 was carved
out of trigger strength, which used to over-reward already-run candles).

Baseline exits are tiered: 🔴 hard stop (overrides), 🟠 HA momentum flip, 🟡 target /
time-stop. On top of that baseline, the shadow book forward-tests **partial scale-outs** (a
conditional 33% booked at the target) and a **Chandelier runner-trail** as parallel
experiment arms (see [How it works](#how-it-works)).

Full design: [`docs/plans/2026-06-14-swing-screener-design.md`](docs/plans/2026-06-14-swing-screener-design.md)
and the target/exit overhaul
[`docs/plans/2026-06-16-target-exit-overhaul-design.md`](docs/plans/2026-06-16-target-exit-overhaul-design.md).

## How it works

```
 universe.csv ─▶ fetch (yfinance, cached) ─▶ resample (1h→4h, 1d→1wk/1mo)
                                                      │
                                                      ▼
                  build_frame (Heiken Ashi + EMA/ATR/RSI/MACD + classification)
                                                      │
        ┌──────────────────────┬──────────────────────┴────────────────┐
        ▼                      ▼                                        ▼
  analyze_frames         analyze_reversals                         score_signal
 (continuation pullback) (oversold bounce)                  (+ MTF alignment, tags)
        │                      │                                        │
        └──────────┬───────────┘                                        ▼
                   ▼                                               shadow book
      ranked signals ─▶ persist (SQL) ─▶ annotated HA charts    (paper-trade every signal
                        ─▶ email digests + on-demand reports      across exit-strategy ARMS,
                                                                   realized R for QC)
```

- The **engine** (`indicators/`, `signals/`, `config.py`) is pure functions over pandas — no
  I/O — so it's fast, deterministic, and trivially testable. A golden test reproduces a known
  AMD 2018 setup.
- The **pipeline** (`data/`, `db/`, `charts/`, `pipeline/`) wraps the engine with data fetch,
  SQL persistence (SQLite locally / Azure SQL in the cloud), chart rendering, universe
  persistence + enrichment, and a nightly orchestrator CLI.
- The **shadow book** fills the *prior* bar's signals against the latest bar (worst-case
  in-zone, no lookahead) and advances open trades through the exit logic, recording outcomes
  in R-multiples. It runs **two orthogonal experiment dimensions**. (1) Exit **arms** —
  `baseline` (all-or-nothing), `partial33_cond` (conditional partial + breakeven runner), and
  `partial33_chand` (partial + Chandelier trail) — open one shared fill under each exit policy
  for a **same-sample A/B** of exits. (2) Screen **variants** (`pipeline/variants.py`) —
  re-screen the prior bar under alternative entry configs (e.g. a tighter freshness gate) and
  book each variant's own fills under the baseline exit, so `breakdown(trades, "variant")` is a
  **strategy leaderboard**. Both feed `analytics/performance.py`.
- **Notifications** (`notify/`) send daily/weekly/monthly digest emails (summary + PDF),
  intraday exit alerts, and **on-demand single-ticker deep analysis** (request a ticker in the
  dashboard → a queued worker runs a multi-timeframe Opus read → emails a PDF → surfaces it
  back in the dashboard). An optional, default-off Opus web-search analyst can enrich digest
  picks.

## Quick start

Requires **Python 3.12**.

```powershell
# from the repo root
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"

# run the test suite
.\.venv\Scripts\python -m pytest -q

# run the screener on a small live slice (writes to a local SQLite DB + charts)
.\.venv\Scripts\python scripts\run_local.py --max-tickers 50
```

Full run instructions, flags, and how to inspect results:
[`docs/running-locally.md`](docs/running-locally.md).

**Dashboard** — a 10-view sidebar app: Overview, Today's Candidates (continuation/reversal
filter + a live **actionability** status — each pick is graded against its latest price as
✅ actionable / 🏃 already ran / ⛔ stopped, already-ran picks hidden by default, and
**repeats** first seen on an earlier run aged out so the same play isn't shown day after day),
Deep Analysis (request on-demand reports), Active Trades (inline close + live P/L),
Trade Entry, Closed Trades (equity curve), Screener Performance (strategy-variant
leaderboard + per-arm exit A/B + score calibration + market-regime cut + play-type
filter), Exit Log, Universe, Digest Log.

```powershell
.\.venv\Scripts\python -m streamlit run src\swing_screener\dashboard\app.py --server.address 127.0.0.1
```

See [`docs/dashboard.md`](docs/dashboard.md).

**Email digests** (summary email + detailed PDF, daily/weekly/monthly, + exit alerts):

```powershell
.\.venv\Scripts\python -m swing_screener.notify.run --kind daily --db sqlite:///local.db
```

See [`docs/email-digests.md`](docs/email-digests.md).

## Project layout

```
src/swing_screener/
  config.py            StrategyConfig — every tunable in one frozen dataclass
  settings.py          env/secrets surface (DB URL, blob, Key Vault, ACS, LLM knobs)
  indicators/          heiken_ashi, trend (EMA/ATR/RSI/MACD)              [pure]
  signals/             classify, frame, detect, entry_zone, fill, exits,  [pure]
                       score, build_score, reversal
  data/                universe (+ S&P 500 seed), resample, fetch         [I/O]
  db/                  models, session, repo (SQLAlchemy + SQLite/mssql)  [I/O]
  charts/              render (annotated Heiken Ashi via mplfinance)      [I/O]
  analytics/           performance (shadow-book QC: expectancy + 95% CI)  [pure]
  pipeline/            analyze (MTF continuation + reversal + score),
                       shadow (multi-arm shadow book), arms (exit arms),
                       variants (screen arms), regime (SPY market context),
                       replay (offline backtest harness), optimize (walk-forward
                       config sweep), propose (auto config-change PR),
                       exitcheck (intraday exit alerts), run (nightly CLI)
  notify/              run, select, analysis (Opus analyst), ondemand
                       (queued deep analysis), ticker_report, pdf, body,
                       acs / smtp / transport (email), alerts
  storage/             blob (Azure Blob for charts/PDFs)                  [I/O]
  dashboard/           app (Streamlit), ui, pl, quotes
tests/                 mirrors src/ — engine + pipeline + db + data + dashboard
scripts/               make_amd_fixture, make_universe_seed, run_local
alembic/               schema migrations (applied on job startup)
infra/                 Bicep IaC (Container Apps Jobs, Azure SQL, Blob, Key Vault)
docs/                  running-locally, dashboard, email-digests, azure-deploy
docs/plans/            design docs + per-phase implementation plans
```

Run the nightly pipeline directly:

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.run --db sqlite:///local.db --cache-dir .cache --chart-dir .charts
```

It is **idempotent per run-date** (safe to re-run a day) and isolates per-ticker failures
(one bad symbol never aborts the run).

**Backtest the screen variants** over cached daily history (offline; prints a leaderboard
ranking each `build_screen_variants` config by expectancy / win rate / fill rate):

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.replay --tickers AMD,NVDA --cache-dir .cache
```

**Sweep + propose a config** (walk-forward: ranks a grid of screen configs on an in-sample
slice, then reports whether the winner holds out-of-sample — the build→measure→optimize loop):

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.optimize --tickers AMD,NVDA --cache-dir .cache
```

A weekly GitHub Actions workflow ([`optimize.yml`](.github/workflows/optimize.yml)) runs this
automatically and, **only** when a swept config beats the shipped gate on a trusted
out-of-sample basis, opens a config-change PR for you to review (`pipeline.propose`) — the
scheduled loop, with a human approval gate (nothing auto-deploys).

## Using the self-optimization loop

The system **measures itself** automatically (every nightly run forward-tests alternate screen
configs in the shadow book and tags each fill with the market regime) and **proposes its own
tuning** — but a human approves the change that ships. Auto-deploying a backtest winner is how
you ship overfit changes against real money, so the approval gate is deliberate. You engage it
three ways, in increasing automation:

1. **See what's working** — open the dashboard's **Screener Performance** page: the strategy
   leaderboard (which screen config is winning, with confidence + sample size), score
   calibration, and the market-regime breakdown. This is the accumulated live evidence; check
   it when you want.
2. **Ask for a recommendation now** — in Claude Code, run **`/tune-screener`** (optionally with
   tickers, e.g. `/tune-screener AMD,NVDA,AAPL`). It runs the optimizer and tells you in plain
   language whether a gate change is worth making *and offers to open the PR* — no flags to
   remember. (The raw version is the `pipeline.optimize` command above.)
3. **Let it propose on a schedule** — the weekly `optimize.yml` workflow opens a config-change
   PR when (and only when) there's a trusted out-of-sample winner. You review and merge.

In all three, **you decide**; the system just does the legwork and shows its work.

## Development

- **Quality gate (also CI):** `ruff check src tests alembic`, `mypy`, `pytest -q` — all must
  pass.
- Built test-first (TDD). The engine stays pure; all I/O lives in `data/`, `db/`, `charts/`,
  `pipeline/`, `notify/`, `storage/`. Tests never hit the network (the fetch + LLM seams are
  mocked) and use temp SQLite + the matplotlib Agg backend.
- **CI/CD:** GitHub Actions runs the gate on every push and PR
  ([`ci.yml`](.github/workflows/ci.yml)); on merge to `main`, CD
  ([`cd.yml`](.github/workflows/cd.yml)) builds the image in ACR and repoints the Azure jobs
  (OIDC federated auth, no stored secret).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Engine core (HA, signals, entry zones, exits, scoring; AMD golden test) | ✅ done |
| 2 | Data pipeline + SQL persistence + shadow book + charts + orchestrator | ✅ done |
| 3 | Streamlit dashboard (10 views: candidates, trades, P/L, performance A/B, deep analysis, …) | ✅ done |
| 4 | Email digests (summary + PDF) + Claude-written analysis + intraday exit alerts | ✅ done |
| 5 | Azure deploy — Container Apps Jobs, Azure SQL, Blob, Key Vault + CD on merge | ✅ deployed |
| — | **Dry-run** — forward-test the shadow-book exit arms to pick a winner | ◻ in progress |
| 6 | Options module (needs a paid data feed) | 📋 later |

Six scheduled jobs run in Azure behind an eastern-time gate: `evening-screen`,
`daily-digest`, `weekly-digest`, `monthly-digest`, `intraday-exit`, and `on-demand-analysis`
— all on one image + one managed identity. The one-time provisioning + cutover is the
[deploy runbook](docs/azure-deploy.md); the exit-arm experiment is tracked in
[`docs/plans/2026-06-16-target-exit-overhaul-design.md`](docs/plans/2026-06-16-target-exit-overhaul-design.md).

## Notes

- Market data is **free yfinance** (with a parquet cache); fine for after-close screening. It
  is unofficial and can be flaky — the fetch layer retries and isolates failures.
- **LLM analysis fails safe:** the digest analyst and on-demand reports degrade to
  deterministic text when the model or web search is unavailable, and the digest analyst is
  **off by default** (`SWING_DEEP_ANALYSIS`).
- This is a personal research/decision-support tool. It **does not place trades**; it surfaces
  setups and forward-tests the strategies. Nothing here is financial advice.
