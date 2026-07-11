# GEX Options Lab — Design

**Date:** 2026-07-11
**Status:** validated with Oliver (architecture, charter, phasing, GEX source, P&L model)
**Source strategy:** Nick Ireland's GEX day-trading method (9/21/50 EMA stacks + gamma-exposure levels + A+ checklist). Reference guide on Oliver's desktop: `GEX_Day-Trading_Strategy_Guide.md`.

## Decision: same repo, firewalled vertical

The GEX module lives in this repo as a sibling vertical — **not** a third play type. A 34-file subsystem mapping (2026-07-11) found the repo's strategy-agnostic machinery (stats core, experiment registry, reflection grader, edge-file pattern, DB/Alembic/Azure chassis, cockpit shell, pure indicators) is directly reusable, while every equity-shaped row and cadence invariant is not. A separate app would rebuild ~60% of hardened infrastructure; extending equity tables would contaminate the learning corpus.

**Hard firewall rules:**

1. Zero writes to equity tables (`Signal`, `PaperTrade`, `ExitEvent`, …). The lab gets parallel tables.
2. No new fields on `StrategyConfig` — the optimizer/variant machinery sweeps it. The lab gets its own `GexConfig`.
3. Equity cluster machinery untouched: the lab re-parameterizes cluster keys, never relaxes `_CLUSTER_FLOOR`.
4. Charter separation: `docs/NORTH_STAR.md` governs the swing system and keeps "not day-trading" as a non-goal. The lab gets `docs/OPTIONS_LAB.md` (learning-first, paper-only, evidence-gated); GEX reflection prompts reason from the lab charter. NORTH_STAR gets only a one-line cross-reference.

## Phasing

- **Phase 1 (this design): prep + journal + grader.** Morning day-plan, manual paper trading graded against the A+ checklist in the cockpit, nightly settlement from completed 5-minute bars. Entirely local — no Azure changes, no live feed, no always-on process.
- **Phase 2 (deferred, designed-for): live engine.** Session-resident process watching 5m bars + GEX levels, auto-detecting setups, alerting in the cockpit. The Phase-1 data model is datetime-keyed and carries nullable contract/premium columns so Phase 2 adds a runtime, not a schema rewrite.

## Key design choices (validated)

| Choice | Decision |
| --- | --- |
| Live role | Phase 1 prep/journal/grader; evolve to live engine later |
| GEX source | Compute in-house (chain snapshot × Black-Scholes gamma × OI), cross-validated against a free public GEX dashboard |
| P&L model | R-multiples on the underlying (stop/target geometry). Premium tracking deferred; schema carries the columns from day 1 |
| Universe | SPY + QQQ (index products with liquid chains via free data; SPX needs CBOE — deferred) |

## Package layout

```
src/swing_screener/options/
  config.py      # GexConfig frozen dataclass — lab tunables, experiment-provenance annotations
  chain.py       # options-chain snapshot via yfinance Ticker.option_chain (mockable seam,
                 #   retry/backoff per fetch.py template); OI + IV per strike/expiry
  gex.py         # PURE: Black-Scholes gamma × OI → per-strike dealer-gamma profile →
                 #   call wall, put wall, gamma flip, net GEX, regime (positive/negative)
  bias.py        # PURE: 9/21/50 EMA stack state (stacked/tangled/slope/spacing) on any frame
  plan.py        # morning day-plan builder: daily bias + GEX map + regime →
                 #   breakout-day vs range-day vs stand-down call
  checklist.py   # the 12-point A+ checklist: item registry, per-item grading, composite verdict
  journal.py     # OptionSetup lifecycle: create → grade → take/skip → close
  settle.py      # nightly settlement: replay session's completed 5m bars,
                 #   resolve stop/target/EOD-flat, write realized_r
  run.py         # CLI: `plan` (pre-market), `settle` (post-close), `validate` (GEX cross-check)
```

Pure modules (`gex.py`, `bias.py`, `checklist.py`) take dataframes/values in, return dataclasses out — no I/O, matching the repo's pure-engine test convention.

### Reused directly

- `indicators/trend.py` `ema()` — the 9/21/50 stack is three calls, timeframe-agnostic.
- `data/resample.py` intraday branch (minute-rule aggregation).
- `data/fetch.py` `fetch_bars(t, "1d")` for the daily bias leg (its in-progress-bar guard already handles pre-market fetches). **Post-close** `fetch_bars(t, "5m")` is safe for settlement — all session bars are complete, so the day-keyed cache invariant holds. Intraday 5m fetches (Phase 2) must bypass this cache with a TTL path; Phase 1 never fetches bars mid-session.
- `db/session.py` engine factory, Alembic chain, repo idempotency patterns, `analytics/performance.py` stats primitives, experiment registry + settlement cards, edge-file machinery, cockpit shell.

## Data model (new tables, new Alembic revisions on the shared chain)

All lifecycle columns are **DateTime** (equity tables are Date-typed — the mapping's #1 incompatibility). All rows carry `account='options-lab'`.

- **GexSnapshot** — `underlying`, `ts`, `spot`, `call_wall`, `put_wall`, `gamma_flip`, `net_gex`, `regime`, per-strike profile (JSON), `source`. One per underlying per morning (refreshable on demand).
- **OptionSetup** (the journal row) — `ts`, `underlying`, `direction`, FK→GexSnapshot, `regime`, pivot level + pattern notes, **12 checklist item booleans** + composite grade, entry/stop/target on the underlying, `status` (idea / taken / skipped), free-text notes.
- **OptionPaperTrade** — FK→OptionSetup, `opened_at`/`closed_at` (DateTime), entry/stop/target, `exit_reason` (stop / target / eod_flat / manual), `realized_r`, `hold_minutes`. **Live-ready nullable columns from day 1:** `occ_symbol` (String(24) — OCC symbols are 21 chars), `strike`, `expiry`, `right`, `contracts`, `entry_premium`, `exit_premium`.

Notes: the shared Alembic head means these revisions will also apply to Azure at the next equity job startup — harmless (empty tables), but sequence revisions carefully. The cockpit change-token gets watermarks for the new tables (the ExitEvent.id lesson); Phase-1 write frequency (morning plan + a handful of journal rows + nightly settle) is low enough for the shared wake channel. Phase 2's live engine gets its **own** SSE channel — the shared wake counter fans out to every panel and must not churn intraday.

## GEX computation & validation

- Snapshot the SPY/QQQ chains pre-market (~9:00–9:15 ET): nearest expiries (0DTE + the next few weeklies), OI and IV per strike.
- Gamma per contract via Black-Scholes from the chain's IV; dealer-gamma convention: calls dealer-long gamma, puts dealer-short (the standard naive GEX model — document the assumption in `gex.py`).
- Derive: call wall (max positive gamma strike above spot), put wall (max put gamma below), gamma flip (zero-crossing of cumulative net GEX), regime.
- `run.py validate` prints our levels alongside instructions to eyeball a free public dashboard; a weekly manual cross-check is the calibration ritual (scraping third-party dashboards is brittle — keep validation semi-manual).
- OI updates once daily pre-market, so a morning-static map is honest; intraday GEX drift is a Phase-2 (paid-data) concern.

## Cockpit: the GEX Lab tab

First view-switcher in `App.tsx` ("Swing" | "GEX Lab" — no router library needed, a top-level state toggle keyed the sanctioned remount way). New `APIRouter` (first router composition in `api.py` — routes stay thin, same 503-friendly DB posture).

Panels:

1. **Day Plan** — bias per underlying (EMA stack state), regime, the three levels vs current spot, breakout/range/stand-down call, "Build today's plan" button (POST → runs plan.py).
2. **Checklist Grader** — the 12-point form; submitting creates an `OptionSetup` with per-item results and computed grade. This is the cockpit's first real user-write surface (precedent: POST /api/azure-login).
3. **Journal** — today's setups + recent history with settled outcomes; mark taken/skipped; manual close with notes.
4. **Lab Stats** — expectancy by grade bucket, by regime, per-checklist-item breakdown — all wearing the `Stat` provenance contract. GEX *levels* are not stats and get a sanctioned plain-value renderer (no fake CIs).

Levels/plan display is text + simple hand-rolled SVG in the existing Sparkline idiom; no charting library in Phase 1.

## Learning loop (lab side)

- **Cluster key = session date, not ticker.** A SPY/QQQ-only book can never clear the equity 8-ticker floor; sessions are also the correct independence unit for day trades. `performance.py`'s bootstrap primitives already take generic cluster→[R] mappings — parameterize, don't fork, and leave equity constants alone.
- `edge/gex.md` + `edge/gex.verdicts.json` following the existing play-type-parametric pattern; reflection prompts reason from `docs/OPTIONS_LAB.md`.
- Pre-registered reflection family (Bonferroni-corrected, keep it small at first): composite grade bucket, gamma regime, and 3–4 highest-hypothesis checklist items (e.g. regime-match, volume confirmation, location-at-pivot). Expanding the family resets K visibly, same as equity.
- Experiments: `scope='gex'` entries in `edge/experiments.json`, settled by the existing state machine.

## Ops (Phase 1: entirely local)

- `python -m swing_screener.options.run plan` pre-market (cockpit button and/or Windows Task Scheduler).
- `python -m swing_screener.options.run settle` post-close (same options).
- **No Azure changes.** No new job in the shared image (blast-radius rule), no yfinance-from-datacenter risk, no heartbeat model changes. A soft "plan built today?" chip in the Lab tab covers liveness. Azure scheduling is a Phase-2 decision gated on the lab proving useful.

## Testing

`tests/options/` mirroring the package: golden-value GEX math on synthetic chains (known walls/flip), EMA-stack state classification, checklist grading, settlement replay against fixture 5m parquet, no-network seams throughout. TDD per repo convention.

## Phase 2 sketch (for the record, not for building now)

Separate always-on local process (not cockpit-hosted — the window closing must not kill the engine) polling 5m bars via a TTL-cached path, evaluating setups against the morning map, writing `idea` journal rows the cockpit surfaces via a dedicated SSE channel; in-window alerts only (pywebview native-call thread trap). Optional intraday chain re-snapshots or a paid feed if morning-static GEX proves too stale. Premium tracking once the setup shows edge.

## Risks accepted

- yfinance chain data (OI/IV) is delayed and occasionally stale — acceptable for a learning lab; the validation ritual bounds it.
- Naive GEX model (no dealer-positioning inference) — same model most free dashboards use; documented assumption.
- Journal discipline is on Oliver — Phase 1 has no auto-detection, so unlogged setups are invisible to the loop. (Phase 2 fixes this.)
- Shared Alembic chain: a bad lab migration could block the equity job's startup migration — mitigated by SQLite + CI migration tests before merge.
