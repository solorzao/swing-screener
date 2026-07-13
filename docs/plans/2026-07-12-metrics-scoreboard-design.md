# Metrics (Scoreboard) screen — design

Date: 2026-07-12
Branch: `feat/metrics-scoreboard`
Status: design approved, pre-implementation

## Motivation

The cockpit today has **no single cross-book metrics dashboard**. Performance
lives in three disjoint places:

- **Mission Control (1)** — `PerformancePanel` over the machine *research* shadow
  corpus (`/api/stats/performance`, `routers/books.py:193`), faceted GOLD/RESEARCH.
  Measures the screener's edge, not account P&L.
- **Journal (11)** — per-book, strictly **firewalled** (`paramsKey=book`), R-native
  (except the robinhood book, which is `$`).
- **Positions & Ledger (3)** — the only dollar surface; a positions ledger, not a
  win/P&L stats view.

Nothing tiles "wins + P&L" across manual / agent / paper the way a trader expects
a scoreboard to. This screen fills that gap **without** weakening the deliberate
"never pool books into one number" stance (NORTH_STAR principle #2): it is the one
sanctioned, clearly-labeled place a cross-book aggregate is allowed to exist.

## Locked decisions (from brainstorming)

1. **Combined view = real-money aggregate.** Pool `manual_equity` (`Trade` table)
   + `live` agent (`PaperTrade account='live'`) — both real money, both R — into one
   figure, labeled as a deliberate cross-book aggregate. Robinhood (`$`) stays out.
2. **R-first, `$` only where it truly exists.** R-multiple is the primary comparable
   metric; real dollars ride as plain sibling fields on the books that have them
   (manual equity, live, robinhood). **No synthetic dollars** for R-only books.
3. **Two manual tiles.** `manual_equity` (R) and `manual_options` / robinhood (`$`)
   are separate tiles in their honest units — never forced into one number.
4. **Paper = the `paper` book only, facet-filtered.** Curated intent book, pinned to
   `would_surface` / baseline arm / default variant. The raw `research` grid is
   **excluded** (it's a signal×arm×variant corpus that over-counts; it keeps its home
   on Mission Control).

Two minor calls taken as defaults: screen name **"Metrics"** (id `metrics`); a single
**combined equity curve in the footer** (per-book curves deferred).

## Tile taxonomy

| Tile | Book(s) | Source | Unit | Metrics |
|---|---|---|---|---|
| Combined real money (hero) | `manual_equity` + `live` | `Trade` ∪ `PaperTrade[account='live']` | R (+ $) | expectancy·CI, win%, W–L, PF, realized $ |
| Manual equity | `manual_equity` | whole `Trade` table | R (+ $) | same |
| Manual options | `robinhood` | `OptionPaperTrade[account='robinhood']` | **$ only** | $ P&L, win%, W–L |
| Live agent | `live` | `PaperTrade[account='live']` | R (+ $) | same (empty today → honest-empty face) |
| Paper | `paper` | `PaperTrade[account='paper']`, `would_surface`, baseline arm, default variant | R | expectancy·CI, win%, W–L, PF |

## Screen placement

New digitless, masthead-linked screen `metrics` in `cockpit-ui/src/lib/screens.ts`
(alongside `journal`/`gexlab`/`systemaudit`). Digitless because the 1–9 keys are a
fixed, muscle-memory contract; a hotkey reassignment is a separate call. Appended to
the registry (display number derives from order — see `screenNumber`). It must be a
**new screen, not a Journal tab**: the Journal is architecturally per-book firewalled
(`paramsKey=book`, one account at a time), and this screen's whole job is to cross
books. Keeping the crossing here is what keeps the firewall honest everywhere else.

## Backend

### New endpoint

`GET /api/stats/scoreboard?window=all|90|180|365` in a new
`cockpit/routers/scoreboard.py`, built as `build_scoreboard_router(_session=...)` and
included in `create_app` (`cockpit/api.py`) like every other router. Read-only; no
`_require_cockpit` guard (it serves stats, takes no action).

Wire shape:

```jsonc
{
  "cards": [
    { "book": "manual_equity", "unit": "R", "expectancy": <Stat|null>,
      "win_rate": 0.55, "n_wins": 18, "n_losses": 15, "n_closed": 33,
      "profit_factor": 1.8, "realized_usd": 2010.0,
      "equity_r": [["2026-01-04", 0.4], ...] },
    { "book": "robinhood", "unit": "$", "expectancy": null,
      "win_rate": 0.48, "n_wins": 12, "n_losses": 13, "n_closed": 25,
      "profit_factor": null, "realized_usd": 1130.0, "equity_r": null },
    { "book": "live", "unit": "R", "expectancy": null, "n_closed": 0, ... },
    { "book": "paper", "unit": "R", "expectancy": <Stat>, ... }
  ],
  "combined": { "books": ["manual_equity", "live"], "unit": "R",
                "expectancy": <Stat|null>, "win_rate": ..., "n_wins": ...,
                "n_losses": ..., "n_closed": ..., "profit_factor": ...,
                "realized_usd": ..., "equity_r": [...] }
}
```

`expectancy` is a full `Stat` (`cockpit/stats.py`) so it renders through `StatChip`
with provenance; it is `null` when the book is `$`-only (robinhood) or empty (live),
where the UI shows a `$` face or an honest-empty face instead.

### The cross-table summarize (the one genuinely new piece)

`analytics.performance.summarize()` is typed `Iterable[PaperTrade]` and gates on
`fill_status == "filled"` + stored `realized_r` (`performance.py:262`,
`_is_closed_filled:185`). The manual `Trade` table has **neither** column — its R is
computed on read as `(exit − entry)/(entry − stop)`. So the combined pool and the
manual-equity card cannot call `summarize()` directly.

Fix (DRY, reuses the exact clustered-bootstrap CI math): **extract the realized-R core
of `summarize()`** into a helper that takes ticker-keyed realized R plus the raw
counts, and have both callers delegate:

```python
def summary_from_realized(
    by_ticker: dict[str, list[float]], *, n_total: int, n_filled: int
) -> PerformanceSummary: ...

# existing PaperTrade path builds by_ticker from closed-filled rows, delegates
def summarize(trades: Iterable[PaperTrade]) -> PerformanceSummary: ...
```

The scoreboard then builds `by_ticker` from **normalized closed results** — one
adapter per source table:

- `Trade` → `(ticker, realized_r or None, realized_usd)` where
  `realized_r = (exit − entry)/(entry − stop)` guarded to `None` on non-positive risk
  (matches `routers/trades.py:296`), `realized_usd = (exit − entry) * size`
  (`_realized_usd`, `trades.py:640`). Reuse the journal's `manual_equity_records`
  (`journal/record.py`) as the source of truth for this mapping so the two never drift.
- `PaperTrade[account='live']` → `(ticker, realized_r, realized_usd)` — `realized_r`
  is stored; `realized_usd` from the ExecutionLog share join (`trades.py:_live_shares`)
  or `None`.

Combined pool = manual results ∪ live results → `summary_from_realized`. Because the
live book is empty today, this transparently degrades to "= manual equity," which the
card states out loud (no hiding, no fabrication).

Robinhood is `$`-only (no stop → no R): its card is computed straight from
`OptionPaperTrade[account='robinhood']` premium P&L (`robinhood_records`,
`record.py:149`), win/loss from `premium_pnl` sign. It never enters any R pool.

### Two supporting changes to `PerformanceSummary`

Add `n_wins: int` and `n_losses: int`. `summarize()` already computes the `wins`/
`losses` lists (`performance.py:273-274`) then discards the counts — so "18W–15L" is
otherwise not truthfully available. Small, additive, and it lets the existing panels
show counts too if desired.

## R-vs-$ contract

`StatChip` remains the ONLY R renderer (design rule 1 — no bare-float renderer).
Dollars ride as plain formatted sibling fields, exactly as `win_rate`/`fill_rate`
already do on the performance panel. A `$`-only card carries `expectancy: null` +
`unit: "$"`; the frontend renders a `$` face with no CI track. There is never a
cross-unit "grand total" mixing R and $.

## Frontend

- `screens/MetricsScreen.tsx` — window `Segmented`; the hero tile; the 4-tile grid;
  the footer combined equity `Sparkline` (cumulative R). `paramsKey=window` drives the
  poll remount (per the cockpit's `usePolling` idiom).
- `components/BookMetricCard.tsx` — one tile. Reuses `StatChip` (R cards), `Lamp`
  (status dot), `Sparkline`. Branches on `unit`: `R` → StatChip + CI; `$` → formatted
  dollars, no chip. `n_closed === 0` → muted honest-empty face; `n < 5` reuses
  StatChip's existing "n<5 — no read" badge; thin (5–11) reuses its THIN treatment.
- `lib/api.ts` — `getScoreboard(window)` + the `ScoreboardCard` / `Scoreboard` wire
  types. `lib/screens.ts` — add `'metrics'` to `ScreenId` + a `SCREENS` row. `App.tsx`
  — mount `MetricsScreen` for the `metrics` id; `Masthead` link picks it up from the
  registry automatically.

## Edge cases (all resolve to an honest face)

- Empty live book → combined == manual equity, captioned; not hidden, not faked.
- Degenerate R (stop ≥ entry, breakeven management) → that trade's `realized_r` is
  `None`, excluded from expectancy but its `$` still counts (matches close logic).
- Robinhood has no `ExitEvent` (bulk-imported already-closed) → read `closed_at`
  directly; win/loss from premium sign; `$` only.
- Thin samples → StatChip n-gating reused verbatim (n<5 no-read, 5–11 THIN).
- Never-pool guardrail → the ONLY aggregate is `manual_equity + live` (both R, both
  real, one labeled endpoint). No code path pools the firewalled journal books; the
  copy states the aggregation.

## Testing

- `summary_from_realized`: parity with `summarize()` on the same data (refactor is
  behavior-preserving); `n_wins`/`n_losses` correctness incl. R==0 excluded from both.
- Scoreboard aggregator: combined = manual ∪ live; live-empty ⇒ combined == manual;
  robinhood excluded from every R pool; degenerate-R rows drop from expectancy but keep
  `$`.
- Endpoint shape: every card present with honest zeros/nulls when a book is empty;
  `window` cut applied.
- Frontend: render of R card, `$`-only card, honest-empty card, and thin/no-read faces.

## Out of scope (YAGNI)

- No synthetic dollars for R-only books.
- No `research` grid tile (over-counts; stays on Mission Control).
- No cross-unit grand total (R + $).
- No combined that crosses into options.
- No new hotkey reassignment (digitless masthead link for now).

## Implementation notes (post-build)

Deviations from this design that surfaced during implementation, recorded for accuracy:

1. **Paper card: gold facet dropped (open item resolved).** The `paper` account
   adapter (`pipeline/execution.py` `PaperAdapter._open`) never stamps `would_surface`
   — that field is only set on the `research` grid. So `facet_filter(paper_rows,"gold")`
   would drop every paper row in production. The paper card instead scopes on
   `account="paper"` closed rows → `arm==BASELINE` → `variant==DEFAULT_VARIANT`
   (the paper book is the curated intent book by construction, so no gold gate is needed).
2. **Frontend has no test framework.** `cockpit-ui` has no vitest/testing-library; the
   UI is verified by `tsc -b` + `oxlint` + driving the built app. The plan's Task 7/8
   component unit tests were replaced by that convention; behaviour was confirmed by
   loading the METRICS screen against a seeded DB.
3. **MetricsScreen owns its own polling.** It takes `wake`, owns the window state, and
   polls `getScoreboard(win)` internally (matching `WeatherScreen`), rather than App
   owning `data`/`window`/`onWindow`. Simpler and matches the codebase convention.
4. **Masthead link is hand-added.** The Masthead lists each digitless screen by hand
   (it does not derive links from the registry), so registering `metrics` also required
   adding a `METRICS` masthead button. Caught by driving the built app (it was otherwise
   unreachable — no digit key and no masthead door).
