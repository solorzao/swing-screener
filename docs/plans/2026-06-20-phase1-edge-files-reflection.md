# Phase 1 — edge files + event-driven reflection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the qualitative loop's first piece — a per-strategy, evidence-graded **playbook** (`edge/<play_type>.md`) maintained by an event-driven **reflection** pass: code deterministically grades conditions into tiered verdicts from the Phase-0 hardened stats, and an Opus seam authors the prose + drafts hypotheses.

**Architecture:** A pure deterministic grader (`pipeline/reflect.py`) runs a small **pre-registered univariate family** (`market_trend`, `score` band, `volatility_tier`) over two evidence sources — the live forward shadow book (→ **forward-confirmed**) and the haircut replay corpus (→ **replay-screened**) — applying a multiple-comparisons-corrected clustered lower bound. The LLM authors the `edge/*.md` from those verdicts (deterministic template fallback). A CLI fires per play type when ≥20 new forward closes accrue (state carried in the edge file's frontmatter — no migration) and a GitHub workflow opens a human-gated PR. Replay gains point-in-time SPY regime stamping so `market_trend` is screenable. Nothing reads or moves price levels.

**Tech Stack:** Python 3.12, numpy, pandas, SQLAlchemy, anthropic (Opus, mockable seam), pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) + [Phase-1 design](2026-06-20-phase1-edge-files-reflection-design.md). Builds on Phase-0 (#41).

---

## Design decisions baked in (from the brainstorm)

- **D1 — Deterministic grading, LLM writes.** Code stamps every confirmed/screened/falsified verdict; the LLM only authors prose + drafts "needs-a-test" hypotheses. It can never override a verdict.
- **D2 — Tiered evidence.** Forward book clears the gate → **forward-confirmed** (gold). Haircut replay corpus clears it → **replay-screened** (candidate). Shown distinctly.
- **D3 — Pre-registered univariate family, MC-aware.** Family = `[market_trend, score-band, volatility_tier]`, frozen in code; the grade clears a Bonferroni one-sided bound over the known family size K (no cross-products).
- **D4 — Regime stamped in replay (point-in-time, no-lookahead)** so `market_trend` is screenable now (chosen over deferring it).
- **D5 — Trigger:** event-driven, ≥20 new closed *forward* trades/play-type since last reflection; state lives in the edge-file frontmatter (git-versioned, no DB migration).
- **D6 — Read-only grading + human-gated PR; levels never touched.**

**Commit discipline:** after each green task run `.venv\Scripts\python.exe -m ruff check .`, `-m mypy`, `-m pytest -q` before committing.

---

## Task 1: MC-corrected clustered bound + forward-book loader (foundation)

**Files:**
- Modify: `src/swing_screener/analytics/performance.py` (`_clustered_ci_low` gains a `lower_pct` param, default 2.5 → byte-identical)
- Modify: `src/swing_screener/db/repo.py` (add a closed-book loader)
- Test: extend `tests/analytics/test_performance.py`, add `tests/db/test_repo_closed_loader.py`

**Step 1 — Failing tests.** (a) `_clustered_ci_low(by_ticker, iid_low, lower_pct=0.5)` returns a LOWER (more conservative) bound than `lower_pct=2.5` on the same data (a stricter percentile → lower bound), and `lower_pct=2.5` is byte-identical to today. (b) `repo.load_closed_paper_trades(session, *, play_type=None, arm=None, variant=None)` returns only closed+filled+realized_r-not-null rows, honoring the optional filters.

**Step 2 — Run, verify fail.**

**Step 3 — Implement.** In `performance.py`, thread a `lower_pct: float = 2.5` param through `_clustered_ci_low` and use it in the `np.percentile(means, lower_pct)` call (summarize keeps calling with the default, so `expectancy_ci_low` is unchanged). In `repo.py`:
```python
def load_closed_paper_trades(
    session: Session, *, play_type: str | None = None, arm: str | None = None,
    variant: str | None = None,
) -> list[PaperTrade]:
    """Filled trades that have closed with a realized result, optionally faceted. The
    reflection grades the LIVE forward book at (arm=BASELINE, variant=DEFAULT_VARIANT)."""
    stmt = select(PaperTrade).where(
        PaperTrade.status == "closed", PaperTrade.fill_status == "filled",
        PaperTrade.realized_r.is_not(None),
    )
    if play_type is not None:
        stmt = stmt.where(PaperTrade.play_type == play_type)
    if arm is not None:
        stmt = stmt.where(PaperTrade.arm == arm)
    if variant is not None:
        stmt = stmt.where(PaperTrade.variant == variant)
    return list(session.scalars(stmt))
```

**Step 4 — Run, verify pass** (incl. the full suite — `expectancy_ci_low` byte-identical, so all Phase-0 tests stay green).

**Step 5 — Commit:** `feat(analytics,repo): tunable clustered-bound percentile + closed-book loader`.

---

## Task 2: Point-in-time regime stamping in replay (unlocks the market_trend tier)

**Files:**
- Modify: `src/swing_screener/pipeline/replay.py` (`replay_book`/`_replay_one` accept + stamp regime)
- Test: extend `tests/pipeline/test_replay_no_lookahead.py`

**Step 1 — Failing tests.** (a) When `replay_book(..., spy_daily=<frame>)` is given a SPY frame, the resulting trades carry non-None `market_trend`/`market_vol`; without it they stay None (back-compat). (b) **No-lookahead:** the regime stamped on a trade opened on date `d` depends only on SPY bars `<= d` — mutating SPY bars AFTER `d` does not change it.

**Step 2 — Run, verify fail.**

**Step 3 — Implement.** Precompute a point-in-time regime map ONCE from SPY (regime is the same for all tickers on a date, so no per-ticker recompute):
```python
def _regime_by_date(spy_daily: pd.DataFrame, cfg: StrategyConfig) -> dict[date, MarketRegime]:
    """Point-in-time SPY regime as-of each date: classify_regime over spy_daily.iloc[:i+1]
    for each i (uses only bars <= that date -- no lookahead). Sparse/missing -> caller
    treats absent dates as unknown (None)."""
    out: dict[date, MarketRegime] = {}
    for i in range(len(spy_daily)):
        out[spy_daily.index[i].date()] = classify_regime(spy_daily.iloc[: i + 1], cfg)
    return out
```
`replay_book`/`_replay_one` take an optional `spy_daily`; build `regime_by_date` once; in `_replay_one`, at each fill look up `regime_by_date.get(fill_date)` and pass `market_trend`/`market_vol` to `open_from_signals` (which already accepts them). Default `spy_daily=None` → pass None (unchanged behavior; the Task-1 golden-master stays green because the default path is byte-identical).

**Step 4 — Run, verify pass.** Verify the no-lookahead test has teeth (mutating future SPY bars must not change a past trade's regime).

**Step 5 — Commit:** `feat(replay): point-in-time SPY regime stamping (no-lookahead)`.

---

## Task 3: The pure deterministic grader

**Files:**
- Create: `src/swing_screener/pipeline/reflect.py` (the family + grader half)
- Test: `tests/pipeline/test_reflect_grade.py`

**Step 1 — Define the frozen family + Verdict type, and the grader.**
```python
from dataclasses import dataclass
from swing_screener.analytics.performance import (
    _CLUSTER_FLOOR, MIN_LEADERBOARD_N, _clustered_ci_low, _is_closed_filled,
)

# PRE-REGISTERED univariate family (D3). Adding a dimension is a deliberate, git-visible
# change that resets K. (key, buckets-or-None); score uses edges, categoricals enumerate.
_SCORE_EDGES = (0.5, 0.6, 0.7, 0.8)
_FAMILY = (
    ("market_trend", ("bull", "bear")),
    ("volatility_tier", ("low", "med", "high")),
    ("score", _SCORE_EDGES),   # handled via score_bucket labels
)
_ALPHA = 0.05            # family-wise; Bonferroni one-sided per bucket
_MARGIN_R = 0.0          # net-of-cost edge must clear zero (after the replay haircut)

@dataclass(frozen=True)
class Verdict:
    play_type: str
    dimension: str        # "market_trend" | "volatility_tier" | "score"
    bucket: str           # e.g. "bull", "high", "0.70-0.80"
    tier: str             # "forward_confirmed" | "replay_screened" | "hunch"
    n: int
    expectancy_r: float
    ci_low: float         # MC-corrected clustered lower bound on the deciding source
    n_clusters: int
    source: str           # "forward" | "replay" | "none"
```
The grader iterates the family, builds the per-bucket realized-R-by-ticker for the forward and replay books, computes the MC-corrected clustered lower bound (one-sided Bonferroni: `lower_pct = 100 * _ALPHA / K`, where K = total buckets in the family), and assigns the tier: **forward_confirmed** if the forward bound > `_MARGIN_R` with n ≥ `MIN_LEADERBOARD_N` and ≥ `_CLUSTER_FLOOR` distinct tickers; else **replay_screened** if the replay bound clears the same bar; else **hunch**. Provide a helper that counts K from `_FAMILY` (so the Bonferroni denominator is the real family size). Falsification (a prior claim now contradicted) is handled in Task 5 against the prior file.

**Step 2-4 — TDD the verdict logic deterministically.** Build forward/replay trade lists with known per-ticker R; assert: a bucket that clears the bound on forward → `forward_confirmed`; one that fails forward but clears replay → `replay_screened`; one that clears the NAIVE 2.5% bound but NOT the Bonferroni `alpha/K` bound → `hunch` (the MC correction has teeth); a bucket below the distinct-ticker floor → never confirmed. Determinism via the seeded bootstrap.

**Step 5 — Commit:** `feat(reflect): pre-registered family + deterministic tiered grader`.

---

## Task 4: Seed the edge files + the structured render/parse + template fallback

**Files:**
- Create: `edge/continuation.md`, `edge/reversal.md` (hand-seeded)
- Modify: `src/swing_screener/pipeline/reflect.py` (render verdicts → markdown; parse frontmatter state)
- Test: `tests/pipeline/test_reflect_render.py`

**Step 1 — Seed files.** Each: a YAML-ish frontmatter (`forward_closed_at_last_reflection: 0`, `last_reflected: null`) + a hand-written **Thesis** (continuation = HA pullback-continuation in an uptrend; reversal = oversold HA bounce) + empty **Confirmed edges / Screened candidates / Hunches / Falsified / Open questions** sections.

**Step 2-4 — TDD render + parse.** A pure `render_edge_file(play_type, thesis, verdicts, prior, n_closed_now) -> str` that lays out the sections (confirmed first, each with n + clustered CI + "net of cost" caveat; screened marked "backtest screen, not confirmed"; falsified carried from prior), and updates the frontmatter `forward_closed_at_last_reflection`. A `parse_state(text) -> ReflectState` reads the frontmatter counter. This render is ALSO the deterministic fallback when the LLM is unavailable. Test the round-trip + that screened entries are clearly labeled not-confirmed + frontmatter updates.

**Step 5 — Commit:** `feat(reflect): seed edge files + deterministic render/parse`.

---

## Task 5: LLM authoring seam (Opus writes the playbook; deterministic fallback)

**Files:**
- Modify: `src/swing_screener/pipeline/reflect.py` (`author_edge_file(...)` with an injectable client)
- Test: `tests/pipeline/test_reflect_author.py`

**Step 1 — Spec.** `author_edge_file(play_type, thesis, verdicts, prior_text, *, client=None) -> str`: builds an Opus prompt with the GROUND-TRUTH verdicts (the LLM must not change tiers/numbers — system prompt states this hard rule), the prior file, and outcome context; Opus returns the revised markdown (phrasing each edge, the qualitative "why," drafting "needs-a-test" hypotheses for ideas no bucket covers, retiring stale items, updating Falsified). Mirror `notify/analysis.py`'s injectable-client + graceful-fallback pattern: on ANY failure, return `render_edge_file(...)` (Task 4 template). The frontmatter counter is set by code AFTER the LLM returns (never trust the model to update state).

**Step 2-4 — TDD with a fake client.** A fake returning canned markdown → used verbatim (+ code-set frontmatter); a fake that raises → deterministic template fallback; assert the prompt includes the verdicts as ground truth and the system prompt forbids changing them. (No network in tests.)

**Step 5 — Commit:** `feat(reflect): Opus authoring seam with deterministic fallback`.

---

## Task 6: Trigger + CLI + the human-gated workflow

**Files:**
- Modify: `src/swing_screener/pipeline/reflect.py` (`due_play_types`, `run_reflection`, `main`)
- Create: `.github/workflows/reflect.yml`
- Test: `tests/pipeline/test_reflect_run.py`

**Step 1 — Trigger + orchestration.** `due_play_types(session, edge_dir) -> list[str]`: for each play type, read the edge file's `forward_closed_at_last_reflection`, count current closed forward trades (`load_closed_paper_trades(session, play_type=pt, arm=BASELINE, variant=DEFAULT_VARIANT)`), and include it if `current - last >= 20`. `run_reflection(session, edge_dir, *, spy_daily, replay_frames, client=None)`: for each due play type, grade (forward book + haircut replay corpus via `replay_book(replay_frames, spy_daily=spy_daily, base_cfg=replace(StrategyConfig(), fill_slippage_atr=_REPLAY_HAIRCUT_ATR))`, filtered to `variant=DEFAULT_VARIANT`, `play_type`), author the file, write it. `main()`: a CLI (load settings/DB, fetch SPY + the replay universe daily from cache) that writes the edge files; mirrors `propose.main`'s "write files, let the workflow open the PR" shape.

**Step 2-4 — TDD `due_play_types` + `run_reflection`** against an in-memory DB seeded with closed forward trades and a temp edge dir, with a fake LLM client and a tiny synthetic replay frame: a play type with ≥20 new closes is due and its file is rewritten; <20 is skipped; the written file's frontmatter counter advances. (CLI/`main` is a thin glue smoke, like propose.)

**Step 5 — Workflow.** `.github/workflows/reflect.yml` mirroring `optimize.yml`: weekly + `workflow_dispatch`, runs `python -m swing_screener.pipeline.reflect`, and `peter-evans/create-pull-request@v6` opens a human-gated PR with `add-paths: edge/` (label `reflection`). Nothing auto-merges.

**Step 6 — Commit:** `feat(reflect): event trigger, CLI, and human-gated reflection workflow`.

---

## Definition of done

- `ruff check . && mypy && pytest -q` green; CI green on the PR.
- The grader is pure + deterministically tested, including the MC correction having teeth (a bucket clearing the naive bound but not the Bonferroni `alpha/K` bound is NOT confirmed) and the distinct-ticker floor.
- Replay regime-stamping is point-in-time with a verified no-lookahead test; the default (no SPY) path is byte-identical (Task-1 golden-master green).
- The LLM authoring has a deterministic template fallback; tests never hit the network.
- Running the CLI on a seeded book rewrites a due play type's `edge/*.md` and opens (in CI) a human-gated PR. **First human-gated reflection PR is the artifact to eyeball** — the LLM's prose quality is reviewed there, not unit-asserted.
- Levels untouched; the reflection only ever opens a PR.

## Out of scope (later phases)

The per-pick insight engine + order intent (Phase 2); execution adapters + conviction calibration (Phase 3); commissioned tests / the agent proposing its own variants for uncovered hunches (Phase 4); condition cross-products; auto-drafting config proposals from confirmed edges.
