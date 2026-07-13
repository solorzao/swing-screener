# Metrics (Scoreboard) Screen Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a new digitless "Metrics" cockpit screen that tiles wins + P&L across the real-money books (manual equity, manual options, live agent) plus a facet-filtered paper book, with a single sanctioned real-money aggregate.

**Architecture:** A read-only `GET /api/stats/scoreboard` endpoint returns one card per book plus a combined `manual_equity + live` aggregate. R cards reuse the existing clustered-bootstrap stats via a refactored `summary_from_realized` core (so the manual `Trade` table, which has no `fill_status`/stored `realized_r`, can share the exact CI math). Dollars ride as plain sibling fields; `StatChip` stays the only R renderer. The frontend adds a `MetricsScreen` + `BookMetricCard`, reusing `StatChip`/`Lamp`/`Sparkline`.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, pytest (backend); Vite + React + TypeScript, vitest (frontend). Companion design: `docs/plans/2026-07-12-metrics-scoreboard-design.md`.

**Conventions for this repo:**
- Type-check in a worktree with `MYPYPATH=src mypy src/swing_screener` (a bare `mypy` checks main's code via the editable install).
- Run backend tests with `python -m pytest`. Frontend from `cockpit-ui/` with `npm run test` (vitest) and `npm run build` (tsc typecheck).
- New `.ts`/`.tsx` files must be committed LF (`.gitattributes` pins `eol=lf`; verify `git diff` shows no CRLF churn).
- Commit after every green task.

---

## Task 1: Add `n_wins` / `n_losses` to `PerformanceSummary`

**Files:**
- Modify: `src/swing_screener/analytics/performance.py` (`PerformanceSummary` dataclass ~151-178; `summarize` return ~314-328)
- Test: `tests/analytics/test_performance.py`

**Step 1: Write the failing test**

```python
def test_summarize_reports_win_and_loss_counts():
    # three closed trades: two winners, one loser (helper builds filled/closed PaperTrades)
    trades = [
        _closed_paper("AAA", realized_r=1.0),
        _closed_paper("BBB", realized_r=0.5),
        _closed_paper("CCC", realized_r=-1.0),
    ]
    s = summarize(trades)
    assert s.n_wins == 2
    assert s.n_losses == 1
```

Reuse whatever closed-`PaperTrade` factory the existing tests in this file already use; do not invent a new one. If none exists, mirror the row shape `_is_closed_filled` requires: `status="closed"`, `fill_status="filled"`, `realized_r` set.

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/analytics/test_performance.py::test_summarize_reports_win_and_loss_counts -v`
Expected: FAIL — `AttributeError: 'PerformanceSummary' object has no attribute 'n_wins'`.

**Step 3: Implement**

In the `PerformanceSummary` dataclass, add two fields after `n_closed`:

```python
    n_closed: int
    n_wins: int
    n_losses: int
```

In `summarize`, the `wins`/`losses` lists already exist (~273-274). Add to the return call:

```python
        n_closed=n_closed,
        n_wins=len(wins),
        n_losses=len(losses),
```

Then grep for every other constructor: `grep -rn "PerformanceSummary(" src tests`. Update each call site (e.g. any zero/empty fixture) with `n_wins=…, n_losses=…`. A count of R==0 is neither a win nor a loss (both lists use strict `>`/`<`), so `n_wins + n_losses` can be `< n_closed`; assert that in the test if you add a break-even row.

**Step 4: Run to verify it passes**

Run: `python -m pytest tests/analytics/test_performance.py -v`
Expected: PASS (whole file, to catch missed constructors).

**Step 5: Commit**

```bash
git add src/swing_screener/analytics/performance.py tests/analytics/test_performance.py
git commit -m "feat(analytics): add n_wins/n_losses to PerformanceSummary"
```

---

## Task 2: Extract `summary_from_realized` core

Behaviour-preserving refactor so any caller with ticker-keyed realized R (not just `PaperTrade`) gets the identical expectancy / CI / PF / win math.

**Files:**
- Modify: `src/swing_screener/analytics/performance.py` (`summarize` body ~262-328)
- Test: `tests/analytics/test_performance.py`

**Step 1: Write the failing test**

```python
def test_summary_from_realized_matches_summarize():
    trades = [
        _closed_paper("AAA", realized_r=1.0),
        _closed_paper("AAA", realized_r=-0.5),
        _closed_paper("BBB", realized_r=2.0),
    ]
    from_trades = summarize(trades)
    by_ticker = {"AAA": [1.0, -0.5], "BBB": [2.0]}
    from_realized = summary_from_realized(by_ticker, n_total=3, n_filled=3)
    assert from_realized.expectancy_r == from_trades.expectancy_r
    assert from_realized.win_rate == from_trades.win_rate
    assert from_realized.expectancy_ci_low == from_trades.expectancy_ci_low
    assert from_realized.n_wins == from_trades.n_wins
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/analytics/test_performance.py::test_summary_from_realized_matches_summarize -v`
Expected: FAIL — `NameError: name 'summary_from_realized' is not defined`.

**Step 3: Implement**

Add the extracted core, then make `summarize` delegate. `avg_hold_bars` is a `PaperTrade`-only concern (no bars in the cross-book view), so it enters as a keyword with a 0.0 default:

```python
def summary_from_realized(
    by_ticker: dict[str, list[float]],
    *,
    n_total: int,
    n_filled: int,
    avg_hold_bars: float = 0.0,
) -> PerformanceSummary:
    """Aggregate stats from ticker-keyed realized R. The shared core of ``summarize``:
    any source (PaperTrade rows, the manual Trade table, a cross-book pool) that can
    produce realized R per ticker gets the identical expectancy / clustered-CI / PF /
    win math. ``avg_hold_bars`` is passed by the PaperTrade caller; sources without a
    bar count leave it 0.0 (it is descriptive, not served)."""
    realized = [r for rs in by_ticker.values() for r in rs]
    n_closed = len(realized)
    fill_rate = n_filled / n_total if n_total else 0.0

    wins = [r for r in realized if r > 0]
    losses = [r for r in realized if r < 0]
    win_rate = len(wins) / n_closed if n_closed else 0.0
    expectancy_r = sum(realized) / n_closed if n_closed else 0.0

    if n_closed >= 2:
        expectancy_stderr = statistics.stdev(realized) / (n_closed ** 0.5)
    else:
        expectancy_stderr = 0.0
    expectancy_ci_high = expectancy_r + _Z95 * expectancy_stderr
    iid_low = expectancy_r - _Z95 * expectancy_stderr
    if n_closed >= 2:
        expectancy_ci_low, n_clusters, thin_clusters = _clustered_ci_low(by_ticker, iid_low)
    else:
        expectancy_ci_low, n_clusters, thin_clusters = iid_low, len(by_ticker), True

    if not n_closed:
        profit_factor = 0.0
    elif losses:
        profit_factor = sum(wins) / abs(sum(losses))
    elif wins:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    return PerformanceSummary(
        n_total=n_total,
        n_filled=n_filled,
        fill_rate=fill_rate,
        n_closed=n_closed,
        n_wins=len(wins),
        n_losses=len(losses),
        expectancy_r=expectancy_r,
        profit_factor=profit_factor,
        avg_hold_bars=avg_hold_bars,
        expectancy_stderr=expectancy_stderr,
        expectancy_ci_low=expectancy_ci_low,
        expectancy_ci_high=expectancy_ci_high,
        n_clusters=n_clusters,
        thin_clusters=thin_clusters,
    )
```

Now shrink `summarize` to build `by_ticker` from closed-filled rows and delegate:

```python
def summarize(trades: Iterable[PaperTrade]) -> PerformanceSummary:
    """Compute aggregate stats over ``trades``. Empty input yields all zeros."""
    trades = list(trades)
    n_total = len(trades)
    n_filled = sum(1 for t in trades if _is_filled(t))
    closed = [t for t in trades if _is_closed_filled(t)]
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        if t.realized_r is not None:
            by_ticker[t.ticker].append(t.realized_r)
    holds = [t.hold_bars for t in closed if t.hold_bars is not None]
    avg_hold_bars = sum(holds) / len(holds) if holds else 0.0
    return summary_from_realized(
        dict(by_ticker), n_total=n_total, n_filled=n_filled, avg_hold_bars=avg_hold_bars
    )
```

Pass `dict(by_ticker)` (a plain dict) so `_clustered_ci_low`/`len` behave identically to before.

**Step 4: Run to verify it passes**

Run: `python -m pytest tests/analytics/test_performance.py -v`
Expected: PASS — the new parity test AND every pre-existing `summarize` test (the refactor must not move a single number).

**Step 5: Commit**

```bash
git add src/swing_screener/analytics/performance.py tests/analytics/test_performance.py
git commit -m "refactor(analytics): extract summary_from_realized core from summarize"
```

---

## Task 3: The scoreboard aggregation module

Pure, session-taking functions that build each book's card and the combined aggregate. Kept out of the router so it is unit-testable without a TestClient.

**Files:**
- Create: `src/swing_screener/cockpit/scoreboard.py`
- Test: `tests/cockpit/test_scoreboard.py`

**Design notes for the implementer:**
- Manual equity R + clustering come from the same mapping the journal uses
  (`journal/record.py:manual_equity_records`): a record's `symbol` is the ticker,
  `result` is realized R (None while open or on degenerate risk). Build `by_ticker`
  from records with `closed is not None and result is not None`.
- Manual equity `$` is not on `TradeRecord`, so sum it straight from `Trade` rows:
  `realized_usd = (exit_price - entry_price) * size` (mirror `routers/trades.py:640`).
  Keep the R formula and this `$` formula pointed at `record.py:125-130` / `trades.py:640`
  with a "keep in sync" comment.
- Live agent = `PaperTrade` rows with `account == "live"`. These are real `PaperTrade`
  objects, so `summarize()` works on them directly for the card; realized `$` needs the
  share join and may be `None` — sum only what resolves.
- Combined = manual `by_ticker` merged with the live rows' `by_ticker`
  (`realized_r` per ticker), fed to `summary_from_realized`. Live-empty ⇒ combined == manual.
- Robinhood card = `robinhood_records` (already `$`, no R): wins/losses from `result`
  sign over closed records, `realized_usd = sum(result)`; `expectancy=None`, `unit="$"`.
- Paper card = `PaperTrade[account="paper"]` closed rows, `facet_filter(rows, "gold")`,
  then `arm == BASELINE`, `variant == DEFAULT_VARIANT`, then `summarize()`.
  ⚠ VERIFY against real data whether the `paper` account stamps `would_surface`; if it
  does not (the intent book may not), drop the `facet_filter` and note it. This is the
  one semantic uncertainty — resolve it with a quick DB check before finalizing.
- A card is a dataclass; the router serializes it. `expectancy` is a `Stat` via
  `stat_from_summary(summary, cost_level=None, corpus_id=None, facet=book, unit="R")`,
  or `None` for `$`-only / empty books.

**Step 1: Write the failing tests (one behaviour each)**

```python
def test_combined_equals_manual_when_live_empty(session):
    _add_manual_trade(session, "AAA", entry=10, stop=9, exit=12, size=100)  # +2R, +$200
    board = build_scoreboard(session, window="all")
    manual = _card(board, "manual_equity")
    assert board["combined"]["n_closed"] == manual["n_closed"]
    assert board["combined"]["expectancy"]["value"] == manual["expectancy"]["value"]
    assert board["combined"]["realized_usd"] == manual["realized_usd"]

def test_robinhood_card_is_dollars_never_r(session):
    _add_robinhood_episode(session, "SPY", premium_pnl=130.0)   # win
    _add_robinhood_episode(session, "QQQ", premium_pnl=-40.0)   # loss
    card = _card(build_scoreboard(session, window="all"), "robinhood")
    assert card["unit"] == "$"
    assert card["expectancy"] is None
    assert card["n_wins"] == 1 and card["n_losses"] == 1
    assert card["realized_usd"] == 90.0

def test_degenerate_r_drops_from_expectancy_keeps_dollars(session):
    _add_manual_trade(session, "AAA", entry=10, stop=10, exit=12, size=100)  # risk=0 -> R None
    card = _card(build_scoreboard(session, window="all"), "manual_equity")
    assert card["n_closed"] == 0            # no R result
    assert card["realized_usd"] == 200.0    # dollars still count
```

Reuse existing cockpit/journal test fixtures for session + row inserts where they exist
(`tests/cockpit/test_journal_api.py`, `tests/cockpit/test_stats.py`). Only add the thin
`_add_*` helpers you actually need.

**Step 2: Run to verify they fail**

Run: `python -m pytest tests/cockpit/test_scoreboard.py -v`
Expected: FAIL — `ModuleNotFoundError: swing_screener.cockpit.scoreboard`.

**Step 3: Implement `build_scoreboard(session, window)`**

Write the module per the design notes above. `window` filters closed rows by
`closed >= today - window_days` (windows "90"/"180"/"365"; "all" = no cut) on each
book's close date, matching the leaderboard window semantics in `books.py:237-239`.
Return the dict wire shape from the design doc (`cards` list + `combined`).

**Step 4: Run to verify they pass**

Run: `python -m pytest tests/cockpit/test_scoreboard.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/swing_screener/cockpit/scoreboard.py tests/cockpit/test_scoreboard.py
git commit -m "feat(cockpit): scoreboard cross-book aggregation (real-money combined)"
```

---

## Task 4: The `/api/stats/scoreboard` endpoint

**Files:**
- Create: `src/swing_screener/cockpit/routers/scoreboard.py`
- Modify: `src/swing_screener/cockpit/api.py` (imports ~48-63; `create_app` includes ~175-213)
- Test: `tests/cockpit/test_scoreboard_router.py`

**Step 1: Write the failing test**

Mirror the TestClient setup in `tests/cockpit/test_api.py`. Assert:

```python
def test_scoreboard_endpoint_shape(client, seeded_session):
    r = client.get("/api/stats/scoreboard")
    assert r.status_code == 200
    body = r.json()
    books = {c["book"] for c in body["cards"]}
    assert books == {"manual_equity", "robinhood", "live", "paper"}
    assert body["combined"]["books"] == ["manual_equity", "live"]
    assert body["combined"]["unit"] == "R"
```

**Step 2: Run to verify it fails**

Run: `python -m pytest tests/cockpit/test_scoreboard_router.py -v`
Expected: FAIL — 404 (route not mounted).

**Step 3: Implement**

`routers/scoreboard.py`, mirroring `build_books_router`'s shape (read-only, no
`_require_cockpit`):

```python
def build_scoreboard_router(*, _session: Callable[[], Iterator[Session]]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/stats/scoreboard")
    def scoreboard(
        window: Literal["all", "90", "180", "365"] = "all",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Cross-book real-money scoreboard. The ONE sanctioned place a cross-book
        aggregate exists (manual_equity + live, both R, both real money) -- every other
        surface stays per-book firewalled. R cards carry a full Stat; $-only (robinhood)
        and empty (live) books carry expectancy null. See docs/plans/2026-07-12-*."""
        return build_scoreboard(session, window=window)

    return router
```

Wire into `api.py`: add the import alongside the others and, next to the other
`app.include_router(...)` calls, `app.include_router(build_scoreboard_router(_session=_session))`.

**Step 4: Run to verify it passes**

Run: `python -m pytest tests/cockpit/test_scoreboard_router.py -v`
Expected: PASS.

**Step 5: Typecheck + commit**

```bash
MYPYPATH=src mypy src/swing_screener/cockpit/scoreboard.py src/swing_screener/cockpit/routers/scoreboard.py
git add src/swing_screener/cockpit/routers/scoreboard.py src/swing_screener/cockpit/api.py tests/cockpit/test_scoreboard_router.py
git commit -m "feat(cockpit): mount /api/stats/scoreboard endpoint"
```

---

## Task 5: Frontend wire types + `getScoreboard`

**Files:**
- Modify: `cockpit-ui/src/lib/api.ts`
- Test: `cockpit-ui/src/lib/api.test.ts` (only if this file has API-shape tests; else skip the test step and rely on `tsc`)

**Step 1: Implement the types + getter**

Mirror an existing `getX` (e.g. `getPerformance`) — same fetch helper, same `ApiError`
handling. Add:

```typescript
export type ScoreboardUnit = 'R' | '$'

export interface ScoreboardCard {
  book: 'manual_equity' | 'robinhood' | 'live' | 'paper'
  unit: ScoreboardUnit
  expectancy: Stat | null
  win_rate: number
  n_wins: number
  n_losses: number
  n_closed: number
  profit_factor: number | null
  realized_usd: number | null
  equity_r: [string, number][] | null
}

export interface Scoreboard {
  cards: ScoreboardCard[]
  combined: {
    books: string[]
    unit: 'R'
    expectancy: Stat | null
    win_rate: number
    n_wins: number
    n_losses: number
    n_closed: number
    profit_factor: number | null
    realized_usd: number | null
    equity_r: [string, number][] | null
  }
}

export const getScoreboard = (window: string): Promise<Scoreboard> =>
  getJson(`/api/stats/scoreboard?window=${window}`)   // use whatever helper getPerformance uses
```

**Step 2: Typecheck**

Run (from `cockpit-ui/`): `npm run build`
Expected: no type errors.

**Step 3: Commit**

```bash
git add cockpit-ui/src/lib/api.ts
git commit -m "feat(cockpit-ui): scoreboard wire types + getScoreboard"
```

---

## Task 6: Register the `metrics` screen

**Files:**
- Modify: `cockpit-ui/src/lib/screens.ts`
- Modify: `cockpit-ui/src/App.tsx` (screen switch + polling)
- Test: none (covered by the render test in Task 8 and manual verification)

**Step 1: Implement**

In `screens.ts` add `'metrics'` to the `ScreenId` union and a row to `SCREENS`
(digitless, appended after `systemaudit` — the display number derives from order):

```typescript
  { id: 'systemaudit', digit: null, title: 'SYSTEM AUDIT' },
  { id: 'metrics', digit: null, title: 'METRICS' },
]
```

In `App.tsx`, mount `MetricsScreen` for the `metrics` id exactly the way `journal` /
`gexlab` are mounted (find the screen switch), and add its polling call if App owns the
poll for screens (mirror how `getPerformance`/`getForwardBooks` are polled with a
`paramsKey`). The `Masthead` link list renders from the registry, so no masthead edit is
needed. `MetricsScreen` does not exist yet — import it; Tasks 7-8 create it (build order:
create the component files first if the compiler blocks you, then wire here).

**Step 2: Typecheck**

Run (from `cockpit-ui/`): `npm run build`
Expected: fails only on the missing `MetricsScreen` import until Task 8 — acceptable
mid-build; do the final green typecheck at the end of Task 8.

**Step 3: Commit** (after Task 8 compiles) — bundle with Task 8's commit.

---

## Task 7: `BookMetricCard` component

**Files:**
- Create: `cockpit-ui/src/components/BookMetricCard.tsx`
- Test: `cockpit-ui/src/components/BookMetricCard.test.tsx`

**Step 1: Write the failing test**

Mirror an existing component test (e.g. a `StatChip`/`PickCard` test). Assert the three
faces:

```tsx
it('renders an R card through StatChip', () => {
  render(<BookMetricCard card={rCard()} />)
  expect(screen.getByText(/18W/)).toBeInTheDocument()
})
it('renders a $-only card with no CI chip', () => {
  render(<BookMetricCard card={dollarCard()} />)
  expect(screen.getByText(/\$/)).toBeInTheDocument()
})
it('renders an honest-empty face when n_closed is 0', () => {
  render(<BookMetricCard card={emptyCard()} />)
  expect(screen.getByText(/no .*fills|—/i)).toBeInTheDocument()
})
```

**Step 2: Run to verify it fails**

Run (from `cockpit-ui/`): `npm run test -- BookMetricCard`
Expected: FAIL — module not found.

**Step 3: Implement**

One tile. Props `{ card: ScoreboardCard | Scoreboard['combined'] }`. Branch:
- `n_closed === 0` → muted honest-empty face (value `—`, caption).
- `unit === '$'` → formatted dollars via `lib/fmt`, win% · `n_wins`W–`n_losses`L, no `StatChip`.
- `unit === 'R'` → `StatChip stat={card.expectancy}` (it self-handles n<5 / THIN),
  plus a sibling line `win {pct} · {n_wins}W–{n_losses}L · PF {pf}` and, when
  `realized_usd != null`, a `$` line. Use `Lamp` for the status dot. Match the tile
  markup/classes in the mockup (`docs/plans/2026-07-12-metrics-scoreboard-design.md`)
  and the cockpit's existing panel CSS conventions.

**Step 4: Run to verify it passes**

Run (from `cockpit-ui/`): `npm run test -- BookMetricCard`
Expected: PASS.

**Step 5: Commit**

```bash
git add cockpit-ui/src/components/BookMetricCard.tsx cockpit-ui/src/components/BookMetricCard.test.tsx
git commit -m "feat(cockpit-ui): BookMetricCard tile (R / \$ / empty faces)"
```

---

## Task 8: `MetricsScreen`

**Files:**
- Create: `cockpit-ui/src/screens/MetricsScreen.tsx`
- Test: `cockpit-ui/src/screens/MetricsScreen.test.tsx`

**Step 1: Write the failing test**

```tsx
it('renders the hero combined tile and four book tiles', () => {
  render(<MetricsScreen data={sampleScoreboard()} window="all" onWindow={() => {}} />)
  expect(screen.getByText(/combined real money/i)).toBeInTheDocument()
  expect(screen.getAllByTestId('book-metric-card')).toHaveLength(5) // hero + 4
})
```

**Step 2: Run to verify it fails**

Run (from `cockpit-ui/`): `npm run test -- MetricsScreen`
Expected: FAIL — module not found.

**Step 3: Implement**

Props `{ data: Scoreboard; window: string; onWindow: (w: string) => void }` (data +
polling owned by `App`, mirroring how the other screens receive their data). Layout:
window `Segmented` (options `all`/`90`/`180`/`365`); hero `BookMetricCard` for
`data.combined`; a grid of `BookMetricCard` over `data.cards` in fixed order
(`manual_equity`, `robinhood`, `live`, `paper`); footer combined equity `Sparkline`
from `data.combined.equity_r`. Give each tile `data-testid="book-metric-card"`.

**Step 4: Run to verify it passes + full typecheck**

Run (from `cockpit-ui/`): `npm run test -- MetricsScreen && npm run build`
Expected: PASS and a clean `tsc` (App.tsx now resolves the import from Task 6).

**Step 5: Commit** (bundle Task 6's screens.ts/App.tsx wiring here)

```bash
git add cockpit-ui/src/screens/MetricsScreen.tsx cockpit-ui/src/screens/MetricsScreen.test.tsx cockpit-ui/src/lib/screens.ts cockpit-ui/src/App.tsx
git commit -m "feat(cockpit-ui): Metrics screen + registry/App wiring"
```

---

## Task 9: Full verification (evidence before done)

**REQUIRED SUB-SKILL:** Use the `verify` skill to drive the real screen, not just tests.

**Step 1: Backend suite + typecheck**

Run: `python -m pytest tests/analytics tests/cockpit -q`
Run: `MYPYPATH=src mypy src/swing_screener`
Expected: all green (this repo runs ~1465 tests clean on main).

**Step 2: Frontend suite + build**

Run (from `cockpit-ui/`): `npm run test && npm run build`
Expected: green; confirm `git diff --stat` shows no CRLF churn on committed static.

**Step 3: Drive the app**

Launch the cockpit (per the `run` skill / the app's launch config), open METRICS from
the masthead, and confirm against a seeded DB: the hero equals manual equity while the
live book is empty; the robinhood tile shows `$` with no CI chip; the paper tile renders
its expectancy; an empty book shows the honest-empty face. Capture a screenshot.

**Step 4: Final review + finish**

Run `/code-review` on the branch diff; address findings. Then use
`superpowers:finishing-a-development-branch` to open the PR.

---

## Notes / open verification items
- **Paper `would_surface`**: confirm the `paper` account stamps `would_surface` before
  keeping the gold `facet_filter` on the paper card (Task 3). If it doesn't, drop it and
  say so in the card caption.
- **Screen number**: `metrics` appends as the last digitless screen. If it should take a
  more prominent slot (or a hotkey), that's a one-line registry reorder — a separate call.
- **Equity curve per book**: deferred; only the combined footer curve ships now.
