# The Journal & Observability Layer — Design

**Date:** 2026-07-11
**Status:** direction approved (all four feature groups); builds AFTER GEX lab Phase 1
**Sources:** TradeZella feature survey (2026), a full concept-mining pass over the owner's prior
project `C:/Users/Oliver/source/repos/Meridian` (2026-07-11, five readers), and the suite's
existing machinery. This layer is **core platform** ([ARCHITECTURE.md](../ARCHITECTURE.md)):
modules carry interchangeable strategies; observability of all trades, P&L, and journaling for
insight analysis belong to the suite.

## What each source contributes

- **The suite (already built):** honest statistics (clustered bootstrap CIs, corpus pinning),
  experiment registry, reflection/verdict machinery, books with fencing, the cockpit shell.
  The engine. TradeZella and Meridian both lack anything like it.
- **TradeZella:** the trader-development UX catalog — P&L calendar, equity curve, discipline
  scoring, playbooks, session review, tags/mistakes, per-trade replay.
- **Meridian:** the decision-record concepts, learned the hard way in a prior build. Its
  best ideas were designed but mostly never wired — this layer is where they finally ship,
  against real data from day one.

## Principles (the Meridian inheritance, adapted)

1. **Time-of-knowledge capture.** Meridian's single best schema decision (migration 002) was
   splitting one `thesis` blob into `entry_thesis` (written before outcome) and `exit_thesis`
   (written at close). Suite version: the *machine* writes the entry thesis — every booked
   trade snapshots its full decision context verbatim at open (setup, gates passed, checklist,
   regime, analyst conviction), immutable from then on; the exit narrative is written at
   settlement. Plan-vs-execution grading falls out for free.
2. **Events, not mutations.** Meridian closed trades by UPDATE-ing the row; the suite already
   learned that lesson (watch `ExitEvent.id`, not the row). Journal artifacts hang off events
   — entry event, exit event, settlement — each written at its own time.
3. **Provenance on every annotation.** Meridian's `TradeStrategyTag.source ('user'|'ai')` is
   the shadow-contamination lesson applied to annotations. Every tag, thesis, note, and
   verdict in this layer records its author: `screener | analyst | human`. Insight analysis
   must be able to separate machine context from human commentary.
4. **Structured + free-text pairs.** A 1–5 scale you can GROUP BY next to prose the LLM reads
   (Meridian's sentiment/conditions pattern). Suite version: snapshot the Market Weather
   regime + VIX bucket as structured fields on every trade at entry; the analyst's prose is
   the free-text twin. "Performance by regime" becomes a query, not archaeology.
5. **Machine auto-populates; the human adds deltas.** Meridian's center of gravity — a human
   typing a form — is the wrong model here. Journal rows are machine-originated; the human
   contributes override reasons, mistake tags, and discretionary notes. `emotional_state`
   exists ONLY on manual actions/overrides (where "FOMO" is genuinely diagnostic), never on
   automated entries.
6. **R-native, unit-honest, never pooled.** All statistics in R-multiples where the book
   supports it; premium-denominated books ($) display in their own unit; cross-book displays
   (the calendar) are per-book or clearly-labeled side-by-side — inference never pools books,
   units never sum.
7. **Staleness stamps.** Every derived artifact (profiles, scores, reports) carries
   `last_refreshed` — the stale-verdicts.json lesson, made structural.

## Data model sketch (platform tables; built with Journal v1)

- **TradeEvent-attached theses:** `entry_thesis` (machine-written JSON+prose snapshot at open,
  immutable) and `exit_thesis` (settlement-time narrative) — added per-book via each book's
  existing rows where the event granularity allows, else a `journal_theses` table keyed
  (book, trade_id, event_kind, source, ts).
- **`journal_tags`** — taxonomy table (kind: `setup | mistake | context`, name, description)
  plus a trade↔tag join carrying `source: screener|analyst|human`. Mistake taxonomy seeds:
  chased, moved_stop, oversized, early_exit, no_setup, revenge.
- **`journal_notes`** — the notebook: day-keyed entries with `kind: premarket | postmarket |
  adhoc`, module scope optional, template-seeded (pre-market auto-seeded from the day plan;
  post-market prompts for review), `source` on every note.
- **Chart snapshots:** persist the as-seen bar/indicator data at signal time and exit
  (data, rendered on demand — cheaper and more honest than recomputing against revised
  history; Meridian stored screenshot blobs, we store the numbers).
- **Unified `TradeRecord` read-model** — a view/query layer over all books
  (`paper_trades`, `option_paper_trades`, future books) normalizing: book, module, symbol,
  direction, opened/closed, unit, r_or_pnl, tags, theses. Display-only; stats stay per-book.

## Feature roadmap

**Journal v1 (first block after GEX Phase 1) — observability + honest analytics:**
- P&L calendar (per book, unit-honest), equity curve + drawdown per book
- MAE/MFE capture (swing book already has `low_water`/`high_water`; GEX settlement records
  excursions while walking 5m bars) + excursion reports — feeds the stop-placement roadmap item
- Discipline metrics: A+-rate over time, per-checklist-item adherence, "sat on my hands" score
- Breakdown reports: by symbol, day-of-week, hold time; time-of-day where DateTime books allow
- Tags + mistakes with provenance; mistake-cost report ("chasing cost 4.2R this month")
- Notebook (pre-market/post-market templates); entry/exit theses wired into GEX + swing books
- Cockpit: a Journal module view — Meridian's analytics blueprint (stat tile row, cumulative
  curve, monthly heatmap, per-strategy table w/ expectancy) rendered with Stat-dict honesty

**Journal v2 — the AI half (Meridian's coach, finally wired):**
- **Session Review agent:** evening auto-draft of the post-market review from the day's
  journal + trades. Prompt contract lifted from Meridian's COACH_PROMPT: patterns in winners
  vs losers with data; behavioral patterns named concretely; process over outcome; cite the
  trader's own trades; no generic advice — aimed at BOTH the human's discretionary behavior
  and the *system's* behavior (gate churn, override frequency, config-change-after-drawdown).
  Default-off, spend-capped, like deep-analysis.
- **Auto trade tagger:** deterministic rules first (chased = entry far from pivot; moved_stop
  = exit worse than plan), LLM suggestions second — always `source='analyst'`, human confirms.
- **Weaknesses profile:** Meridian's designed-never-built `weaknesses: ["exits_winners_early",
  ...]` list — a periodic distillation job over settled trades producing a compact,
  staleness-stamped profile artifact injected into analyst/coach context (the existing
  edge-file pattern, aimed at the trader).
- Per-trade chart with entry/stop/target/exit markers (mplfinance exists); trade detail view
- Edge Score: Zella-Score-style composite radar (win rate, profit factor, avg W/L, drawdown,
  consistency, discipline) — wearing thin-data honesty labels theirs doesn't have

**Later / on demand:** bar-replay deliberate practice, additional broker importers (the
BrokerFill contract generalizes), daily pre-market AI briefing (API-spend decision).

## Explicitly not ported from Meridian

Chat-first agent UX (pipelines with scheduled outputs beat three chat rooms); auto-learned
screener criteria from top-P&L trades (the overfitting trap the suite's corpus-pinning exists
to prevent — learning from settled trades routes through the experiment machinery only); RAG
over trade embeddings (SQL + loop-stats wins at this scale); manual trade entry as the primary
flow; dollar-first headline stats; win_rate without sample-size awareness; SaaS scaffolding
(auth tiers, rate limits, multi-user); the hold-days trading-style classifier; mockup-first
pages (everything ships wired to real data, as the cockpit already does).
