# Swing Screener

Automated screener for a **Heiken Ashi pullback-continuation** swing-trading strategy.
It scans a universe of stocks after the close, finds setups across multiple timeframes,
ranks and categorizes them, renders annotated charts, and forward-tests every signal in a
self-grading **shadow book** so the strategy can be measured and improved with real data.

> Status: **Phases 1–2 complete** (the engine + a runnable local pipeline). Phases 3–5
> (dashboard, email/LLM digests, Azure) are designed and planned — see [Roadmap](#roadmap).

---

## The strategy

A trend-continuation **long** entry, evaluated independently on **4h / Daily / Weekly /
Monthly** bars (the timeframe sets the expected hold length):

1. **Uptrend context** — `EMA20 > EMA50` and price above `EMA50`.
2. **Shallow pullback** — a bearish Heiken Ashi "shaved head" then a few red / small-body
   "zone" candles that hold above `EMA50` (a continuation, not a reversal).
3. **Trigger** — the most recent *closed* bar flips bullish (the first strong green HA
   candle out of the pullback).

Each signal carries an ATR-scaled **entry zone** `[floor, ceiling]`, a **stop**, and a
**target**, plus categorization tags (timeframe→horizon, quality, volatility, oversold) and
a composite **score** used for ranking. Exits are tiered: 🔴 hard stop (overrides), 🟠 HA
momentum flip, 🟡 target / time-stop.

Full design: [`docs/plans/2026-06-14-swing-screener-design.md`](docs/plans/2026-06-14-swing-screener-design.md).

## How it works

```
 universe.csv ─▶ fetch (yfinance, cached) ─▶ resample (1h→4h, 1d→1wk/1mo)
                                                      │
                                                      ▼
                              build_frame (Heiken Ashi + EMA/ATR/RSI + classification)
                                                      │
                  ┌───────────────────────────────────┼───────────────────────────────┐
                  ▼                                   ▼                                 ▼
          detect_last_bar                       compute_zone                       score_signal
       (pullback trigger)                 (entry zone/stop/target)            (+ MTF alignment, tags)
                  │                                                                     │
                  ▼                                                                     ▼
        ranked signals ─▶ persist (SQLite) ─▶ annotated HA charts (top N)        shadow book
                                                                              (paper-trade every
                                                                               signal, tiered exits,
                                                                               realized R for QC)
```

- The **engine** (`indicators/`, `signals/`, `config.py`) is pure functions over pandas —
  no I/O — so it's fast, deterministic, and trivially testable. A golden test reproduces a
  known AMD 2018 setup.
- The **pipeline** (`data/`, `db/`, `charts/`, `pipeline/`) wraps the engine with data fetch,
  SQLite persistence, chart rendering, and a nightly orchestrator CLI.
- The **shadow book** fills the *prior* bar's signals against the latest bar (worst-case
  in-zone, no lookahead) and advances open trades through the exit logic, recording outcomes
  in R-multiples — the data you use to judge and tune the screener.

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

**Dashboard** (browse candidates, track trades + live P/L, review screener performance):

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
  indicators/          heiken_ashi, trend (EMA/ATR/RSI)              [pure]
  signals/             classify, frame, detect, entry_zone, fill,    [pure]
                       exits, score, build_score
  data/                universe (+ S&P 500 seed), resample, fetch    [I/O]
  db/                  models, session, repo (SQLAlchemy + SQLite)   [I/O]
  charts/              render (annotated Heiken Ashi via mplfinance)  [I/O]
  pipeline/            analyze (per-ticker MTF + score + tags),
                       shadow (the shadow book),
                       run (nightly orchestrator CLI)
tests/                 mirrors src/ — engine + pipeline + db + data + charts
scripts/               make_amd_fixture, make_universe_seed, run_local
docs/plans/            design doc + per-phase implementation plans
```

Run the nightly pipeline directly:

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.run --db sqlite:///local.db --cache-dir .cache --chart-dir .charts
```

It is **idempotent per run-date** (safe to re-run a day) and isolates per-ticker failures
(one bad symbol never aborts the run).

## Development

- **Quality gate (also CI):** `ruff check src tests`, `mypy`, `pytest -q` — all must pass.
- Built test-first (TDD). The engine stays pure; all I/O lives in `data/`, `db/`, `charts/`,
  `pipeline/`. Tests never hit the network (the fetch seam is mocked) and use temp SQLite +
  the matplotlib Agg backend.
- **CI:** GitHub Actions runs the gate on every push and PR (`.github/workflows/ci.yml`).

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Engine core (HA, signals, entry zones, exits, scoring; AMD golden test) | ✅ done |
| 2 | Data pipeline + SQLite persistence + shadow book + charts + orchestrator | ✅ done |
| — | **Local dry-run** (run nightly ~1–2 weeks to accumulate the shadow book) | ◻ in progress |
| 3 | Local Streamlit dashboard (candidates, active trades, P/L, screener performance) | ✅ done |
| 4 | Email digests (summary + detailed PDF) + Claude-written analysis + exit alerts | ✅ done |
| 5 | Deploy to Azure (Container Apps Jobs, Azure SQL, Blob, Key Vault) + CD on merge | 🚧 code done · deploy pending |
| 6 | Options module (needs a paid data feed) | 📋 later |

Plans live in [`docs/plans/`](docs/plans/). Phase 5's code is implemented and tested
(mssql-aware engine, Alembic migrations, Key-Vault secrets, blob-backed charts, the
container ENTRYPOINT gate, the Bicep IaC under [`infra/`](infra/), and the OIDC CD
workflow); the one-time Azure provisioning + cutover is run from the
[deploy runbook](docs/azure-deploy.md).

## Notes

- Market data is **free yfinance** (with a parquet cache); fine for after-close screening.
  It is unofficial and can be flaky — the fetch layer retries and isolates failures.
- This is a personal research/decision-support tool. It **does not place trades**; it
  surfaces setups and forward-tests the strategy. Nothing here is financial advice.
