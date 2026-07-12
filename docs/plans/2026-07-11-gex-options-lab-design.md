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
4. Charter separation. *(Superseded 2026-07-11 by the Meridian governance pass: `docs/NORTH_STAR.md` is now the suite-level constitution, swing specifics moved to `docs/modules/swing-screener.md`, and the lab charter landed as `docs/modules/gex-lab.md` — not `docs/OPTIONS_LAB.md` as originally written here.)* The principle stands: the lab has its own charter (learning-first, paper-only, evidence-gated) whose scope neither binds nor is bound by the swing module's; GEX reflection prompts reason from the suite North Star + the lab charter.

## Phasing

- **Phase 1 (this design): prep + journal + grader.** Morning day-plan, manual paper trading graded against the A+ checklist in the cockpit, nightly settlement from completed 5-minute bars. Entirely local — no Azure changes, no live feed, no always-on process.
- **Phase 2 (deferred, designed-for): live engine.** Session-resident process watching 5m bars + GEX levels, auto-detecting setups, alerting in the cockpit. The Phase-1 data model is datetime-keyed and carries nullable contract/premium columns so Phase 2 adds a runtime, not a schema rewrite.

## Key design choices (validated)

| Choice | Decision |
| --- | --- |
| Live role | Phase 1 prep/journal/grader; evolve to live engine later |
| GEX source | Compute in-house (chain snapshot × Black-Scholes gamma × OI), cross-validated against a free public GEX dashboard |
| P&L model | R-multiples on the underlying (stop/target geometry). Premium tracking deferred; schema carries the columns from day 1 |
| Universe | SPY + QQQ default watchlist for the morning plan; **ad-hoc GEX analysis accepts any optionable ticker**, guarded by a chain-liquidity check (SPX needs CBOE — deferred) |
| Broker import | Robinhood activity-CSV importer: parse → **review grid with per-episode GEX/other/skip tagging** → commit to a separate `robinhood` book; human-confirmed linking to journaled setups. Format pinned against Oliver's real 2026-07-11 export |

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
  broker_import.py  # Robinhood activity-CSV parser → BrokerFill rows → FIFO pairing
                    #   into round-trip trades; setup-link suggestions
  run.py         # CLI: `plan` (pre-market), `settle` (post-close), `analyze <ticker>`
                 #   (ad-hoc map; also serves the dashboard cross-check — the separate
                 #   `validate` subcommand was folded into it), `import-robinhood <csv>`
```

Pure modules (`gex.py`, `bias.py`, `checklist.py`) take dataframes/values in, return dataclasses out — no I/O, matching the repo's pure-engine test convention.

### Reused directly

- `indicators/trend.py` `ema()` — the 9/21/50 stack is three calls, timeframe-agnostic.
- `data/resample.py` intraday branch (minute-rule aggregation).
- `data/fetch.py` `fetch_bars(t, "1d")` for the daily bias leg (its in-progress-bar guard already handles pre-market fetches). **Post-close** `fetch_bars(t, "5m")` is safe for settlement — all session bars are complete, so the day-keyed cache invariant holds. Intraday 5m fetches (Phase 2) must bypass this cache with a TTL path; Phase 1 never fetches bars mid-session.
- `db/session.py` engine factory, Alembic chain, repo idempotency patterns, `analytics/performance.py` stats primitives, experiment registry + settlement cards, edge-file machinery, cockpit shell.

## Data model (new tables, new Alembic revisions on the shared chain)

All lifecycle columns are **DateTime** (equity tables are Date-typed — the mapping's #1 incompatibility). Book fencing: paper-lab trades carry `account='options-lab'`, imported episodes `account='robinhood'`; the snapshot/setup/fill tables are module-fenced by being lab-only tables (no account column).

- **GexSnapshot** — `underlying`, `ts`, `spot`, `call_wall`, `put_wall`, `gamma_flip`, `net_gex`, `regime`, per-strike profile (JSON), `source`. One per underlying per morning (refreshable on demand).
- **OptionSetup** (the journal row) — `ts`, `underlying`, `direction`, FK→GexSnapshot, `regime`, pivot level + pattern notes, **12 checklist item booleans** + composite grade, entry/stop/target on the underlying, `status` (idea / taken / skipped), free-text notes.
- **OptionPaperTrade** — FK→OptionSetup, `opened_at`/`closed_at` (DateTime), entry/stop/target, `exit_reason` (stop / target / eod_flat / manual / expired; imported episodes use sold / expired), `realized_r`, `hold_minutes`. **Live-ready nullable columns from day 1:** `occ_symbol` (String(24) — OCC symbols are 21 chars), `strike`, `expiry`, `right`, `contracts`, `entry_premium`, `exit_premium`. Also serves imported broker trades (see Robinhood import): `account` distinguishes the books (`options-lab` paper vs `robinhood`), a `strategy` tag (`gex` / `other` — set at review time, editable later) scopes which imported episodes the GEX stats see, and imported rows carry a nullable FK→OptionSetup that is only set when Oliver confirms a link. One row per flat-to-flat **episode** (all fills in a contract from first open to net-zero), not per fill.
- **BrokerFill** — one immutable row per imported CSV transaction: `import_hash` (unique — dedup key over the raw row fields **plus a per-content occurrence ordinal**, so byte-identical duplicate fills stay distinct while hashes remain stable across overlapping re-exports; see the implementation plan Task 10), `activity_date` (date — the export has no time component), `underlying`, `occ_symbol`, `trans_code` (BTO/STC/STO/BTC/OEXP), `quantity`, `price`, `amount` (fee-inclusive), `raw` (JSON of the source line), `source='robinhood'`. Fills persist even when their episode is skipped at review — dedup and later re-review both need them; episode pairing is re-runnable from fills.

Notes: the shared Alembic head means these revisions will also apply to Azure at the next equity job startup — harmless (empty tables), but sequence revisions carefully. The cockpit change-token gets watermarks for the new tables (the ExitEvent.id lesson); Phase-1 write frequency (morning plan + a handful of journal rows + nightly settle) is low enough for the shared wake channel. Phase 2's live engine gets its **own** SSE channel — the shared wake counter fans out to every panel and must not churn intraday.

## GEX computation & validation

- Snapshot the SPY/QQQ chains pre-market (~9:00–9:15 ET): nearest expiries (0DTE + the next few weeklies), OI and IV per strike.
- Gamma per contract via Black-Scholes from the chain's IV; dealer-gamma convention: calls dealer-long gamma, puts dealer-short (the standard naive GEX model — document the assumption in `gex.py`).
- Derive: call wall (max positive gamma strike above spot), put wall (max put gamma below), gamma flip (zero-crossing of cumulative net GEX), regime.
- `run.py analyze` prints our levels alongside instructions to eyeball a free public dashboard (the planned separate `validate` subcommand was folded into `analyze`); a weekly manual cross-check is the calibration ritual (scraping third-party dashboards is brittle — keep validation semi-manual).
- OI updates once daily pre-market, so a morning-static map is honest; intraday GEX drift is a Phase-2 (paid-data) concern.

### Any-ticker analysis + liquidity guard

The GEX pipeline is ticker-agnostic — `run.py analyze NVDA` (or the cockpit's "Analyze ticker" input) snapshots any optionable chain and produces the same map. But dealer-gamma levels are only *meaningful* on dense chains where hedging flow actually operates. `GexConfig` carries liquidity thresholds (minimum total OI near spot, minimum populated strike density; a spread-width check is Phase 2 — the Phase-1 chain snapshot doesn't retain bid/ask); a chain that fails them still renders, wearing a prominent **"thin chain — levels unreliable"** warning rather than being blocked. The automatic morning plan runs only the configured watchlist (`GexConfig.watchlist`, default `("SPY", "QQQ")` — editable); ad-hoc analyses store `GexSnapshot` rows like any other so a journaled setup on TSLA links to the map that motivated it.

## Cockpit: the GEX Lab tab

First view-switcher in `App.tsx` ("Swing" | "GEX Lab" — no router library needed, a top-level state toggle keyed the sanctioned remount way). New `APIRouter` (first router composition in `api.py` — routes stay thin, same 503-friendly DB posture).

Panels:

1. **Day Plan** — bias per watchlist underlying (EMA stack state), regime, the three levels vs current spot, breakout/range/stand-down call, "Build today's plan" button (POST → runs plan.py), plus an **"Analyze ticker"** input for ad-hoc maps on any symbol (thin-chain warning surfaced inline).
2. **Checklist Grader** — the 12-point form; submitting creates an `OptionSetup` with per-item results and computed grade. This is the cockpit's first real user-write surface (precedent: POST /api/azure-login).
3. **Journal** — today's setups + recent history with settled outcomes; mark taken/skipped. *(Phase 1 ships exactly that; manual close with notes, tag re-editing, Robinhood link confirm/reject, and the `needs review` queue are deferred to Phase 1.5 — see the implementation plan's deferred list.)*
4. **Lab Stats** — expectancy overall + by grade bucket wearing the `Stat` provenance contract *(by-regime and per-checklist-item breakdowns: Phase 1.5, with the reflection family)*, and a separate Robinhood-book section — **plain labeled premium-P&L values, not Stat dicts** (the charter: a labeled display, not pooled inference; planned-vs-realized arrives with the linking UI in Phase 1.5). GEX *levels* are likewise plain values (no fake CIs).
5. **Import** — CSV file-picker (client-side `FileReader` → JSON `csv_text` POST — the multipart idea was dropped to avoid a python-multipart dependency) opening the **review grid**: parsed episodes with contract, date span, contracts, fee-inclusive P&L, and day-trade/multi-day badges; per-episode GEX / other / skip tagging plus setup-link confirmation; commit summary (episodes committed by tag, fills deduped, still-open episodes carried).

Levels/plan display is text + simple hand-rolled SVG in the existing Sparkline idiom; no charting library in Phase 1.

## Robinhood import

Closes the loop between what was journaled and what was actually traded — the discipline half of the strategy ("did I sit on my hands?"). Import is a **two-stage flow: parse → human review/tag → commit.** Nothing reaches the book without Oliver's review, because not every Robinhood trade is a GEX trade.

**Format (pinned against Oliver's real export, 2026-07-11).** Columns: `Activity Date, Process Date, Settle Date, Instrument, Description, Trans Code, Quantity, Price, Amount`, all quoted. Facts the parser must honor:

- Dates are M/D/YYYY with **no time component** — everything downstream works at day granularity.
- Option descriptions parse as `TICKER M/D/YYYY Call|Put $STRIKE`; expiration rows are `Trans Code=OEXP` with description `Option Expiration for …`, a quantity that may carry a suffix (`30S`), and empty price/amount.
- Money fields: `$`-prefixed prices; amounts with thousands commas and parentheses for debits (`($1,320.04)`). **Amounts include fees** (e.g. 5 × $0.05 × 100 → `($25.20)`), so summed amounts give true P&L net of fees — use amounts for P&L, `Price` for per-contract premium.
- Non-trade rows to skip: `ACH` (deposits/cancels), `RTP` (instant transfers + withdrawal fees) — rows with empty `Instrument`.
- Trailer garbage to tolerate: a bare `""` line and a disclaimer row with a *mismatched column count*.
- Trade codes present: `BTO`, `STC`, `OEXP`; parser also accepts `STO`/`BTC` (short/close) and flags them `needs review` rather than pairing naively.
- One order fragments into many partial-fill lines (same date/contract/price) — the parser stores each as its own `BrokerFill` but presents them aggregated.

**Pairing → episodes.** Fills group per contract into **flat-to-flat episodes**: from first opening fill until net position returns to zero (FIFO internally for lot accounting). The episode is the reviewable, taggable, stat-bearing unit — one `OptionPaperTrade(account='robinhood')` row per episode with VWAP entry/exit premium, total contracts, fee-inclusive P&L, and first-open/last-close dates. Fragmented fills, scale-ins, and scale-outs collapse into one honest trade; a multi-week accumulation reads as one episode, a same-day scalp as another. Episodes still open at import time (net position ≠ 0) commit as `open` and settle on a later import.

**Review & GEX tagging (Oliver's requirement).** The Lab tab's Import panel shows parsed episodes — contract, date span, contracts, real P&L, day-trade vs multi-day badge — each with a three-way decision: **GEX / other / skip**. Skipped episodes are not committed (their fills still land in `BrokerFill` for dedup and re-review). Committed episodes carry `strategy='gex'|'other'`; Lab Stats default to `strategy='gex'`, and a comparison stat (GEX trades vs the rest of the book) shows whether the system is actually improving results. Tags remain editable in the Journal — a review-time decision is never final. CLI equivalent: `run.py import-robinhood <csv>` parses, stores fills, and prints the episode table; the cockpit review grid is the tagging surface (`--tag-all` is the only CLI commit path — interactive prompting was dropped).

**Setup linking.** With date-only precision, link suggestions match underlying + same session date against `OptionSetup` rows; ambiguities (two setups that day) are resolved by hand in the review grid. A linked pair lets stats compare *planned* R (underlying geometry) against *realized* premium P&L — slippage, early exits, and theta made visible per trade.

**Book separation.** The `robinhood` book is premium-denominated and never pools with the paper lab's underlying-R stats; each book gets its own facets and Lab Stats section. Equity (share) rows are ignored in Phase 1. **The raw export is never committed to the repo** (it contains bank-transfer lines); the test fixture is an anonymized reconstruction preserving every format quirk above.

## Learning loop (lab side)

- **Cluster key = session date, not ticker.** A SPY/QQQ-only book can never clear the equity 8-ticker floor; sessions are also the correct independence unit for day trades. `performance.py`'s bootstrap primitives already take generic cluster→[R] mappings — parameterize, don't fork, and leave equity constants alone.
- `edge/gex.md` + `edge/gex.verdicts.json` following the existing play-type-parametric pattern; reflection prompts reason from the suite North Star + `docs/modules/gex-lab.md`.
- Pre-registered reflection family (Bonferroni-corrected, keep it small at first): composite grade bucket, gamma regime, and 3–4 highest-hypothesis checklist items (e.g. regime-match, volume confirmation, location-at-pivot). Expanding the family resets K visibly, same as equity.
- Experiments: `scope='gex'` entries in `edge/experiments.json`, settled by the existing state machine.

## Ops (Phase 1: entirely local)

- `python -m swing_screener.options.run plan` pre-market (cockpit button and/or Windows Task Scheduler).
- `python -m swing_screener.options.run settle` post-close (same options).
- **No Azure changes.** No new job in the shared image (blast-radius rule), no yfinance-from-datacenter risk, no heartbeat model changes. A soft "plan built today?" chip in the Lab tab covers liveness. Azure scheduling is a Phase-2 decision gated on the lab proving useful.

## Testing

`tests/options/` mirroring the package: golden-value GEX math on synthetic chains (known walls/flip), EMA-stack state classification, checklist grading, settlement replay against fixture 5m parquet, importer round-trips against an anonymized fixture reconstructing every quirk of the real export (fragmented fills, OEXP `30S` quantities, parenthesized comma amounts, ACH/RTP noise rows, mismatched-column trailer; dedup on re-import, episode pairing, STO/BTC flagging), no-network seams throughout. TDD per repo convention.

## Phase 2 sketch (for the record, not for building now)

Separate always-on local process (not cockpit-hosted — the window closing must not kill the engine) polling 5m bars via a TTL-cached path, evaluating setups against the morning map, writing `idea` journal rows the cockpit surfaces via a dedicated SSE channel; in-window alerts only (pywebview native-call thread trap). Optional intraday chain re-snapshots or a paid feed if morning-static GEX proves too stale. Premium tracking once the setup shows edge.

## Risks accepted

- yfinance chain data (OI/IV) is delayed and occasionally stale — acceptable for a learning lab; the validation ritual bounds it.
- Naive GEX model (no dealer-positioning inference) — same model most free dashboards use; documented assumption.
- Journal discipline is on Oliver — Phase 1 has no auto-detection, so unlogged setups are invisible to the loop. (Phase 2 fixes this.)
- Shared Alembic chain: a bad lab migration could block the equity job's startup migration — mitigated by SQLite + CI migration tests before merge.
