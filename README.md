# Meridian

A personal **trading suite**: interchangeable strategy **modules** on permanent shared
infrastructure — data seams, one database, honest statistics and learning loops, a
journal/observability layer, a desktop cockpit, and a locked-down execution arc.

**▶ [North Star](docs/NORTH_STAR.md) — the suite's constitution; every decision in every
module is checked against it.** In one line: *remove emotional risk by building a systemic,
evidence-based edge — found the way a disciplined trader finds one (observe → hypothesize →
test → adjust → re-evaluate) — that ultimately executes autonomously, but only edges it has
proven, sized by a rule, under hard limits a human can always override.*
**▶ [Architecture & module contract](docs/ARCHITECTURE.md)** — what the platform provides and
what every module must bring.
**▶ [Using Meridian — the operator's guide](docs/using-meridian.md)** — what *you* do with it:
the first two weeks, the daily and weekly loops, and where to look when something's off. Start
here if you want to *use* the system rather than understand its internals.

| Module | Charter | Status |
|---|---|---|
| **Swing screener** (documented below) | [docs/modules/swing-screener.md](docs/modules/swing-screener.md) | live in production |
| **GEX options lab** | [docs/modules/gex-lab.md](docs/modules/gex-lab.md) | Phase 1 in build |

*(The repo/package keeps the `swing-screener` name until a rename earns its cost — see
ARCHITECTURE's naming policy. The rest of this README documents module 1 and the shared
platform it runs on.)*

### Module 2: the GEX options lab (Phase 1)

A local, **paper-only** lab for learning the GEX day-trading method (9/21/50 EMA stacks +
dealer-gamma levels + a 12-point A+ checklist). Phase 1 is a prep/journal/grader — no live
feed, no Azure job, no execution path exists. CLI (`python -m swing_screener.options.run`):

| Command | What it does |
|---|---|
| `plan` | pre-market: compute the SPY/QQQ GEX map (in-house, chain OI × Black-Scholes gamma) + daily EMA bias → a breakout / range / stand-down day plan |
| `analyze <ticker> [--save]` | ad-hoc GEX map for any optionable ticker, with a thin-chain warning |
| `settle` | post-close: replay the session's completed 5-minute bars to resolve open lab trades to R-multiples |
| `import-robinhood <csv> [--tag-all=gex\|other]` | import a Robinhood activity export into a separate premium book, review-and-tag which trades were GEX |

Charter: [docs/modules/gex-lab.md](docs/modules/gex-lab.md) · playbook: [edge/gex.md](edge/gex.md).
The cockpit **GEX LAB** screen is live (masthead link, or the `g x` chord): day plan,
checklist grader, setup journal, lab stats, broker import, and the settle sweep.

## Module 1: the swing screener

A self-improving **swing-trading system**. It screens a universe of stocks for **Heiken Ashi**
setups, forward-tests every signal in a self-grading **shadow book**, *learns* which setups
actually work (two coupled edge-finding loops sharing a per-strategy playbook), turns each pick
into a conviction-graded, concretely-sized **order intent**, and runs that intent through a
pluggable **execution adapter** — a human-placeable ticket, an Alpaca paper position, or
(behind hard locks) a real-money order. It screens two complementary **long** plays —
trend-**continuation** pullbacks and oversold-bounce **reversals** — and runs locally or as
scheduled Azure Container Apps Jobs.

> **Status:** the full arc is built and deployed. The original **screener foundation** (engine →
> pipeline + shadow book → cockpit → digests → Azure) runs as seven scheduled jobs; the
> **learning loop** (honest stats → edge-file playbooks → the insight engine) and the
> **execution arc** (adapters → Alpaca paper → armable-when-ready real money) are live.
> **Default posture is `off`** — it moves no money until a human deliberately arms it. See
> [The system at a glance](#the-system-at-a-glance) and the [Roadmap](#roadmap).

---

## The system at a glance

Three layers, built in that order, each standing on honest measurement:

1. **The screener** — finds setups across timeframes, ranks + categorizes them, renders charts,
   emails digests, and **forward-tests every signal** in a shadow book (paper-trades it, grades
   the outcome in R-multiples). This is the evidence everything else stands on.

2. **The learning loop** — two coupled edge-finding loops sharing one living, evidence-graded
   **playbook per strategy** (`edge/<strategy>.md`):
   - **Quantitative** — replay → a significance-ranked leaderboard → a walk-forward optimizer →
     a human-gated config-change PR. The system *measures itself* and *proposes its own tuning*.
   - **Qualitative** — an Opus **insight engine** that, per pick, says where the trade sits vs.
     the strategy's proven edges + failure modes, layers external context, **forms a conviction
     it can move (with a reason)**, and hands over a concrete order intent — plus an event-driven
     **reflection** pass that maintains the playbooks. Every analyst call is logged and
     **scored against the realized outcome**, so its judgment earns a track record.

3. **The execution arc** — the `OrderIntent` flows through a pluggable adapter:
   `manual` (a human-placeable ticket — place it yourself on Robinhood), `paper` (a simulated
   position), or `live` (a real Alpaca order). Real money is fenced behind **three independent
   locks + hard caps + an advisory autonomy gate**; turning it on is a deliberate **human flip**,
   never something the code does on its own.

**Money safety is the spine of all of it:** the default is `off`; the LLM never moves a price
level or grades what ships; nothing arms real money without a human acting on purpose. See
[Execution & money safety](#execution--money-safety).

## What runs automatically (the daily / weekly cadence)

Deployed, the system runs **ten scheduled Azure Container Apps Jobs** (one image, one managed
identity) behind an **Eastern-time gate** — the UTC crons fire on both EST and EDT, and the gate
(`ops/eastern_gate.py`) lets each job proceed only at the right ET hour (and, for the monthly
digest, only on the last business day). Unattended, day to day:

| Job | Cadence (ET) | What it does on its own |
|---|---|---|
| `evening-screen` | every weekday, ~4pm (after close) | the full screen → persist + charts → advance the shadow book → (deep on) the insight engine + order intents → execution-adapter dispatch (`off` by default) |
| `daily-digest` | every weekday, ~8am | emails the daily digest (summary + PDF) + the reversal Top-3 + (deep on) the order intents + the autonomy-gate countdown |
| `intraday-exit` | weekdays, hourly 9am–4pm | checks open trades for exit triggers → emails exit alerts (deduped per event) |
| `on-demand-analysis` | hourly | drains the cockpit's deep-analysis request queue |
| `weekly-digest` | Fridays, ~4pm | the weekly digest |
| `monthly-digest` | last business day of the month, ~4pm | the monthly digest |
| `market-weather` | Sundays, ~9am | the weekly macro **Market Weather** report (MTF SPY regime + VIX/yields → analyst email) |
| `journal-coach` | hourly | drains the Personal Trade Coach's on-close review-draft queue + refreshes the weekly Weaknesses Profile |
| `journal-audit-weekly` | Saturdays, ~4pm | the System Behavior Auditor's weekly conduct report |
| `journal-audit-breach` | weekdays, ~4pm | the Auditor's daily breach scan (caps exceeded, disarms) |

Plus two **weekly, human-gated** GitHub Actions that open a PR for you to review — **nothing
auto-merges**:

| Workflow | Cadence | What it proposes |
|---|---|---|
| [`optimize.yml`](.github/workflows/optimize.yml) | Sundays 06:00 UTC | the walk-forward config sweep → a **config-change PR** *only* on a trusted out-of-sample winner |
| [`reflect.yml`](.github/workflows/reflect.yml) | Sundays 07:00 UTC | re-grades each strategy's recent outcomes → an **edge-file reflection PR** (the playbook update) |

So on its own the system **screens every weekday afternoon, digests every morning, watches exits
hourly, and proposes its own improvements weekly** — while it **deliberately moves no money**:
execution stays `off` until a human arms it (see [Execution & money safety](#execution--money-safety)).
The one-time provisioning + cutover is the [deploy runbook](docs/azure-deploy.md).

## The strategies

Two independent **long** screeners run per ticker on **4h / Daily / Weekly / Monthly** bars (the
timeframe sets the expected hold length). Every signal carries a `play_type`, an ATR-scaled
**entry zone** `[floor, ceiling]`, a **stop**, a **target**, categorization tags (horizon,
quality, volatility, oversold), and a composite **score** used for ranking.

### Continuation pullback (the primary play)

1. **Uptrend context** — `EMA20 > EMA50` and price above `EMA50`.
2. **Shallow pullback** — a bearish Heiken Ashi "shaved head" then a few red / small-body "zone"
   candles that hold above `EMA50` (a continuation, not a reversal).
3. **Trigger** — the most recent *closed* bar flips bullish (the first strong green HA candle out
   of the pullback).
4. **Freshness (anti-chase)** — the trigger is rejected if its close already sits more than
   `max_extension_atr` (default **2.0**) ATR above `EMA20`. A setup that has already run far off
   the pullback is a chase, not an entry, so it never reaches the screen *or* the shadow book.
   Empirically, fresh pullback triggers fire under ~1 ATR of extension; late chases fire at ~2.7.

The continuation **target** is structure-aware: the nearest standard-candle swing-high resistance
above the entry ceiling, else an ATR measured-move (`reference + 2·ATR`), floored at **1.5R** so
reward:risk stays sane.

### Reversal plays (oversold bounce)

A counter-trend complement: a run of **≥3 red HA candles below `EMA50`** that flips green — tagged
`early` (fresh flip) or `confirmed` (flip + follow-through). Entry is a **0.382–0.618 pullback**
into the bounce, stop below the bounce origin, target at the **0.786 retrace** toward the prior
breakdown. Surfaced as a "Reversal Plays" list in the daily digest and a Continuation/Reversal
filter throughout the cockpit.

### Scoring & exits

The composite **score** blends HA trigger strength (0.25), multi-timeframe alignment (0.20),
trend slope (0.15), volatility fit (0.10), **RSI** bull-range pullback quality (0.15),
**MACD-histogram** momentum (0.05), and **freshness** (0.10).

Baseline exits are tiered: 🔴 hard stop (overrides), 🟠 HA momentum flip, 🟡 target / time-stop.
On top of that baseline, the shadow book forward-tests **partial scale-outs** (a conditional 33%
booked at the target) and a **Chandelier runner-trail** as parallel experiment arms.

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
                        ─▶ INSIGHT ENGINE ─▶ OrderIntent           realized R for QC)
                                              │
                                              ▼
                              execution adapter (manual / paper / live)
```

- The **engine** (`indicators/`, `signals/`, `config.py`) is pure functions over pandas — no
  I/O — so it's fast, deterministic, and trivially testable. A golden test reproduces a known
  AMD 2018 setup.
- The **pipeline** (`data/`, `db/`, `charts/`, `pipeline/`) wraps the engine with data fetch,
  SQL persistence (SQLite locally / Azure SQL in the cloud), chart rendering, universe
  persistence + enrichment, and a nightly orchestrator CLI.
- The **shadow book** fills the *prior* bar's signals against the latest bar (worst-case in-zone,
  no lookahead) and advances open trades through the exit logic, recording outcomes in
  R-multiples. It runs **two orthogonal experiment dimensions**: exit **arms** (`baseline`,
  `partial33_cond`, `partial33_chand` — a same-sample A/B of exits) and screen **variants**
  (re-screen the prior bar under alternative entry configs — a strategy leaderboard). Both feed
  `analytics/performance.py`.
- **Notifications** (`notify/`) send daily/weekly/monthly digest emails (summary + PDF), intraday
  exit alerts, and **on-demand single-ticker deep analysis**.

## The learning loop

The system finds and grows its edge the way a disciplined trader does — and writes down what it
learns. The shared artifact is a **playbook per strategy** (`edge/continuation.md`,
`edge/reversal.md`): thesis, **confirmed edges**, hunches / needs-a-test, **falsified / retired**,
open questions — versioned in git (the history *is* the trading journal), hand-editable, and
revised through a human-gated PR.

- **Honest measurement first.** Every claim carries its sample size, a **ticker-clustered**
  confidence interval, and is *net of costs* — with multiple-comparisons control and a
  label-shuffle placebo so a non-signal can't pass (`analytics/performance.py`,
  `pipeline/propose.py`). This is the floor everything reasons from.
- **The deterministic grader + reflection** (`pipeline/reflect.py`) grades each strategy's recent
  outcomes into tiered verdicts — *forward-confirmed* (proven on the live shadow book),
  *replay-screened*, *hunch* — and an Opus authoring seam writes the playbook prose (it never
  grades; code owns the numbers). Runs weekly as a human-gated PR ([`reflect.yml`](.github/workflows/reflect.yml)).
- **The insight engine** (`pipeline/insight.py`, `notify/analysis.py`) turns the deep-analysis LLM
  from a narrator into a **learning analyst**. Per pick it computes a deterministic baseline
  **conviction** from where the trade sits in the playbook, lets the analyst **move it ±1 (code-
  clamped) with a stated reason**, and produces a concrete, conviction-scaled, R-based
  **`OrderIntent`**. Every call is persisted (`AnalystCall`) and **scored against the pick's
  realized shadow outcome**, so the reflection can review whether the analyst's judgment is
  proving out (the calibration is code-owned, never the LLM's say-so).
- **The quantitative loop** measures itself and proposes its own tuning — see
  [Using the self-optimization loop](#using-the-self-optimization-loop).

## Execution & money safety

The `OrderIntent` flows through a pluggable **execution adapter**, selected by a single
fail-safe setting `SWING_EXECUTION_MODE` (default **`off`** = today's render-only behavior; an
unknown value coerces to `off`):

| mode | adapter | what it does | money |
|---|---|---|---|
| `off` | NoOp | renders the pick; nothing executes | none |
| `manual` | `ManualAdapter` | records an exact order **ticket** + a **"Proposed orders"** list you place by hand (e.g. on Robinhood) | none |
| `paper` | `PaperAdapter` | opens a *simulated* position the shadow stepper fills/closes | none |
| `live` | `LiveAdapter` | submits a real order to **Alpaca**, reconciled from broker fills | real (locked) |

- **The intent book is isolated by construction.** A first-class `account` dimension
  (`research` | `paper` | `live`) on every trade keeps the curated execution book out of the
  research aggregates, so live trades never pollute the leaderboard or the calibration.
- **The broker owns live fills.** A `live` order is submitted, then **reconciled** from the
  broker's real fill price (`pipeline/reconcile.py`) — never simulated; the bar-stepper never
  touches a live position.
- **Real money needs three independent locks + hard caps**, checked in code before any
  real-money order: `SWING_EXECUTION_MODE=live` **and** `SWING_BROKER_ALLOW_REAL_MONEY=yes`
  **and** the advisory **autonomy gate** reads `ready` — plus per-day notional / loss /
  max-concurrent caps must all be set. A paper endpoint bypasses these (fake money); an unknown
  broker host is treated as real money (fail-safe).
- **The autonomy gate is advisory and read-only** (`pipeline/autonomy.py`). It runs a real
  **calibration test with teeth** — does `high`-conviction out-earn `low`, on a clustered CI
  where a placebo can't pass? — and surfaces a **countdown** to the floors (20+20 scored calls
  across ≥8 tickers). It **never** flips `execution_mode`; it only tells you whether autonomy
  *may be considered*. It cannot pass for a while yet (calibration data accrues with closed
  trades) — that's by design.
- **Arming is a deliberate human flip.** When the gate reads ready, you run a read-only
  **preflight** GO/NO-GO check (`python -m swing_screener.pipeline.preflight`), then set the live
  config by hand following [`docs/runbooks/arming-alpaca-live.md`](docs/runbooks/arming-alpaca-live.md).
  The **kill switch** is the same act in mirror: set `SWING_EXECUTION_MODE=off` (re-read before
  every order) to halt the next order and cancel resting ones.
- **Robinhood stays human-in-the-loop.** A sourced feasibility spike found Robinhood's agentic
  MCP can't authenticate headlessly for an unattended cron (interactive desktop auth, undocumented
  token longevity, ToS lockout risk), so Robinhood is **not** an autonomous backend — the
  `manual` mode's "Proposed orders" surface is how you act on a pick there, by hand. **Alpaca**
  (static keys, real paper sandbox) is the autonomous broker.

```
gate ready?  ──no──▶  keep accruing calibration data (watch the countdown)
     │ yes
     ▼
preflight GO?  ──no──▶  fix the failing check
     │ yes
     ▼
human sets:  execution_mode=live + allow_real_money=yes + live host/keys + caps + funded
     ▼
real orders place on Alpaca   (kill switch: execution_mode=off)
```

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

**Desktop cockpit** — a native desktop app (a pywebview window, or a browser tab) over the same
database, launched from the Start menu or the CLI. Mission Control plus screens for Candidates
(continuation/reversal + a live **actionability** status — each pick graded against its latest
close as ✅ actionable / 🏃 already ran / ⛔ stopped), Positions & Ledger (log/close trades,
live P/L, equity curve), Playbooks, Analyst, Deep Analysis (request on-demand reports),
Execution Safety (the DISARM venue sweep), Market Weather, and Reference (Universe, Digest Log,
the filterable Exit Log). It replaces the retired Streamlit dashboard.

```powershell
.\.venv\Scripts\python -m swing_screener.cockpit --browser
```

See [`docs/cockpit.md`](docs/cockpit.md).

**Email digests** (summary email + detailed PDF, daily/weekly/monthly, + exit alerts):

```powershell
.\.venv\Scripts\python -m swing_screener.notify.run --kind daily --db sqlite:///local.db
```

See [`docs/email-digests.md`](docs/email-digests.md).

**Read the learning + execution state** (all read-only, no money):

```powershell
# advisory autonomy gate + the calibration countdown to the floors
.\.venv\Scripts\python -m swing_screener.pipeline.autonomy

# go/no-go readiness check before arming a live broker (reachable / funded / caps / gate)
.\.venv\Scripts\python -m swing_screener.pipeline.preflight
```

## Using the self-optimization loop

The system **measures itself** automatically (every nightly run forward-tests alternate screen
configs in the shadow book and tags each fill with the market regime) and **proposes its own
tuning** — but a human approves the change that ships. Auto-deploying a backtest winner is how you
ship overfit changes against real money, so the approval gate is deliberate. You engage it three
ways, in increasing automation:

1. **See what's working** — open the desktop cockpit's **Performance** panel: the strategy
   leaderboard (with confidence + sample size), score calibration, and the market-regime breakdown.
2. **Ask for a recommendation now** — in Claude Code, run **`/tune-screener`** (optionally with
   tickers). It runs the optimizer and tells you in plain language whether a gate change is worth
   making *and offers to open the PR*.
3. **Let it propose on a schedule** — the weekly [`optimize.yml`](.github/workflows/optimize.yml)
   workflow opens a config-change PR when (and only when) there's a trusted out-of-sample winner.

In all three, **you decide**; the system just does the legwork and shows its work.

## Project layout

```
src/swing_screener/
  config.py            StrategyConfig — every tunable in one frozen dataclass
  settings.py          env/secrets surface (DB, blob, KV, ACS, LLM, execution mode + locks/caps)
  indicators/          heiken_ashi, trend (EMA/ATR/RSI/MACD)                 [pure]
  signals/             classify, frame, detect, entry_zone, fill, exits,     [pure]
                       score, build_score, reversal
  data/                universe (+ S&P 500 seed), resample, fetch, quotes    [I/O]
  db/                  models, session, repo (SQLAlchemy + SQLite/mssql)     [I/O]
  charts/              render (annotated Heiken Ashi via mplfinance)         [I/O]
  analytics/           performance (clustered-CI shadow-book QC),            [pure]
                       calibration (the autonomy gate's conviction test), pl
  pipeline/            analyze, shadow (multi-arm shadow book), arms, variants,
                       regime, replay (offline backtest), optimize (walk-forward),
                       propose (auto config PR), exitcheck, run (nightly CLI),
                       reflect (deterministic grader + edge-file reflection),
                       insight (baseline conviction + R-sizing + OrderIntent),
                       execution (NoOp/Manual/Paper/Live adapters + limits),
                       broker / broker_alpaca (BrokerClient + Alpaca paper/live),
                       reconcile (live fills → positions), autonomy (advisory gate
                       + countdown), preflight (read-only go/no-go)
  notify/              run, select, analysis (Opus analyst + conviction nudge),
                       ondemand, ticker_report, pdf, body, proposals
                       ("Proposed orders" surface), acs / smtp / transport, alerts
  storage/             blob (Azure Blob for charts/PDFs)                      [I/O]
  cockpit/             FastAPI app factory + routers (the desktop UI backend) [I/O]
edge/                  per-strategy playbooks (continuation.md, reversal.md)
cockpit-ui/            React + TS frontend, built into cockpit/static
tests/                 mirrors src/ — engine + pipeline + db + data + cockpit
scripts/               make_amd_fixture, make_universe_seed, run_local
alembic/               schema migrations (applied on job startup)
infra/                 Bicep IaC (Container Apps Jobs, Azure SQL, Blob, Key Vault)
docs/                  using-meridian (the operator's guide), running-locally,
                       cockpit, email-digests, azure-deploy
docs/runbooks/         arming-alpaca-live (the deliberate human flip to real money)
docs/plans/            NORTH_STAR-governed design docs + per-phase implementation plans
```

Run the nightly pipeline directly (idempotent per run-date; isolates per-ticker failures):

```powershell
.\.venv\Scripts\python -m swing_screener.pipeline.run --db sqlite:///local.db --cache-dir .cache --chart-dir .charts
```

## Development

- **Quality gate (also CI):** `ruff check src tests alembic`, `mypy`, `pytest -q` — all must pass.
- Built test-first (TDD). The engine stays pure; all I/O lives in `data/`, `db/`, `charts/`,
  `pipeline/`, `notify/`, `storage/`. Tests **never hit the network** (the fetch, LLM, and broker
  seams are mocked — a `FakeBroker` / `httpx.MockTransport` means the suite can never place an
  order) and use temp SQLite + the matplotlib Agg backend.
- **CI/CD:** GitHub Actions runs the gate on every push and PR ([`ci.yml`](.github/workflows/ci.yml));
  on merge to `main`, CD ([`cd.yml`](.github/workflows/cd.yml)) builds the image in ACR and
  repoints the Azure jobs (OIDC federated auth, no stored secret).

## Roadmap

**Foundations** — the screener, built and deployed:

| # | Scope | Status |
|---|---|---|
| 1 | Engine core (HA, signals, entry zones, exits, scoring; AMD golden test) | ✅ done |
| 2 | Data pipeline + SQL persistence + shadow book + charts + orchestrator | ✅ done |
| 3 | Streamlit dashboard | ✅ done |
| 4 | Email digests (summary + PDF) + Claude analysis + intraday exit alerts | ✅ done |
| 5 | Azure deploy — Container Apps Jobs, Azure SQL, Blob, Key Vault + CD | ✅ deployed |

**The learning + execution arc** ([North Star](docs/NORTH_STAR.md)), built on the foundation:

| Phase | Scope | Status |
|---|---|---|
| 0 | Loop-statistics hardening (clustered CI, MC control, placebo, no-lookahead) | ✅ shipped |
| 1 | Per-strategy edge-file playbooks + event-driven reflection | ✅ shipped |
| 2 | The insight engine — conviction + R-sized OrderIntents + the calibration loop | ✅ shipped |
| 3 | Execution adapters (manual/paper) + the advisory autonomy gate | ✅ shipped |
| 4 | The live broker arc — Alpaca paper backend + reconcile (armable-when-ready) | ✅ shipped |
| 5 | Robinhood approval surface + Alpaca arming readiness (preflight, runbook, countdown) | ✅ shipped |
| — | The **human flip** to autonomous Alpaca real money | ◻ gated on the calibration data + a deliberate human act |

What's open next: a true Robinhood `review → place` MCP integration (human-in-the-loop), a
partial/fractional fill model, native bracket/OCO exit orders, and letting the analyst propose its
own variants. **Autonomy is always the last step, and always a human's.**

## Notes

- Market data is **free yfinance** (with a parquet cache); fine for after-close screening. It is
  unofficial and can be flaky — the fetch layer retries and isolates failures.
- **The LLM never moves money or grades what ships.** It annotates, sizes, and nudges conviction
  (within a code-clamped ±1, logged + outcome-scored); the deterministic rules engine owns every
  price level, and hardened statistics — never the model's confidence — gate any promotion or
  real-money arming. The digest analyst and on-demand reports **fail safe** to deterministic text
  and are **off by default** (`SWING_DEEP_ANALYSIS`).
- **On money:** the default posture is `off` and the system moves **no money** until a human
  deliberately arms it (paper first, real money last, behind three locks + hard caps + the
  autonomy gate). It can place **paper** orders on Alpaca and, once armed, **real** ones — but
  arming is always a conscious human act with a kill switch. This is a personal
  research/decision-support tool; **nothing here is financial advice.**
