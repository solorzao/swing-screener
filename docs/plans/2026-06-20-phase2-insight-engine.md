# Phase 2 — the insight engine (a learning analyst) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Per candidate, produce a real insight + a conviction-graded, concretely-sized **order intent** — where the conviction is a deterministic playbook *baseline* the Opus analyst can **nudge with a reason**, and every analyst call is **logged and scored against the realized outcome** so its judgment earns a track record (North Star #9).

**Architecture:** A deterministic core (`pipeline/insight.py`: baseline conviction from the grader's verdicts + R-based sizing + the `OrderIntent` type) is anchored to evidence; the Opus seam (in `notify/analysis.py`) moves the grade ±1 with a logged reason and writes the prose, falling back to the baseline on any failure. The reflection writes a machine-readable `edge/<pt>.verdicts.json` the baseline reads (never the LLM prose). An `AnalystCall` store records each call; a scoring step joins it to the pick's shadow-book outcome → the analyst's own conviction calibration, reviewed by the Phase-1 reflection. Levels stay deterministic; execution stays manual.

**Tech Stack:** Python 3.12, anthropic (mockable seam), SQLAlchemy + Alembic, numpy, pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) (#1 scoped to promotion/money; #9 the analyst's learning role) + [Phase-2 design](2026-06-20-phase2-insight-engine-design.md). Builds on Phase-0 (#41) + Phase-1 (#43).

---

## Design decisions baked in
- **D1 — Conviction = baseline (code) + nudge (LLM, ±1, logged reason).** The baseline is deterministic from the playbook verdicts; the analyst genuinely moves it but is anchored. On any LLM failure → the baseline.
- **D2 — The analyst learns: every call logged + scored (A).** `AnalystCall` rows are scored against the pick's shadow outcome → the analyst's conviction calibration; the reflection promotes proven recurring observations, surfaces empty confidence.
- **D3 — Baseline reads MACHINE-READABLE verdicts, not LLM prose.** The reflection emits `edge/<pt>.verdicts.json` (deterministic, from `grade()`); the insight engine reads that.
- **D4 — Levels are ground truth; money never auto-moves.** Decision-support only; execution manual until Phase 3. Sizing/conviction advise; they never set a level.

**Commit discipline:** after each task, gate on green (`.venv\Scripts\python.exe -m ruff check .`, `-m mypy`, `-m pytest -q`) BEFORE committing.

---

## Task 1: Machine-readable verdicts sidecar

**Files:** Modify `src/swing_screener/pipeline/reflect.py` (serialize verdicts in `run_reflection`); Test `tests/pipeline/test_reflect_verdicts_json.py`.

**Step 1 — Failing tests.** `verdicts_to_json(verdicts) -> str` round-trips through `load_verdicts(text) -> list[Verdict]` (all fields preserved). `run_reflection(...)` writes `edge/<pt>.verdicts.json` next to `edge/<pt>.md` for each reflected play type, parseable by `load_verdicts`.

**Step 2-4 — Implement.** Add `verdicts_to_json` (json.dumps of `[asdict(v) for v in verdicts]`) + `load_verdicts(text_or_path)`; in `run_reflection`, after computing `verdicts`, also `(edge_dir / f"{pt}.verdicts.json").write_text(verdicts_to_json(verdicts))`. Pure serialization (no behavior change to the markdown path). The JSON is the **deterministic source of truth** for Phase-2 conviction; the markdown stays the human view.

**Step 5 — Commit:** `feat(reflect): emit machine-readable edge verdicts sidecar`.

---

## Task 2: Deterministic baseline conviction + R-based sizing (the pure core)

**Files:** Create `src/swing_screener/pipeline/insight.py`; Test `tests/pipeline/test_insight_core.py`.

**Step 1 — Define types + pure functions.**
```python
from dataclasses import dataclass
from swing_screener.pipeline.reflect import Verdict, _SCORE_EDGES
from swing_screener.analytics.performance import _score_labels

_CONVICTIONS = ("avoid", "low", "medium", "high")
_MULT = {"high": 1.0, "medium": 0.5, "low": 0.25, "avoid": 0.0}

@dataclass(frozen=True)
class OrderIntent:
    ticker: str; timeframe: str; play_type: str
    entry_floor: float; entry_ceiling: float; stop: float; target: float
    conviction: str; shares: int; risk_dollars: float
    edge_played: str; key_risk: str; insight: str

def _score_band(score: float) -> str:
    labels = _score_labels(_SCORE_EDGES)
    idx = len(_SCORE_EDGES)
    for i, edge in enumerate(_SCORE_EDGES):
        if score < edge:
            idx = i; break
    return labels[idx]

def conviction_baseline(*, score, volatility_tier, market_trend, verdicts) -> tuple[str, str]:
    """Deterministic baseline conviction + the edge it keys on, from the pick's buckets vs the
    playbook verdicts. avoid if any matching condition is falsified; else high if a matching
    condition is forward_confirmed; else medium if replay_screened; else medium (neutral)."""
    conds = {"score": _score_band(score), "volatility_tier": volatility_tier,
             "market_trend": market_trend}
    matches = [v for v in verdicts if v.bucket == conds.get(v.dimension)]
    # Falsification isn't a Verdict tier in Phase 1 (handled in the file); treat a matching
    # forward_confirmed with NEGATIVE ci_low as an avoid signal. Phase-2 keeps it simple:
    if any(m.tier == "forward_confirmed" and m.ci_low < 0 for m in matches):
        return "avoid", _edge_label(next(m for m in matches if m.ci_low < 0))
    conf = [m for m in matches if m.tier == "forward_confirmed"]
    if conf:
        return "high", _edge_label(max(conf, key=lambda m: m.ci_low))
    screened = [m for m in matches if m.tier == "replay_screened"]
    if screened:
        return "medium", _edge_label(max(screened, key=lambda m: m.ci_low))
    return "medium", "no matching playbook edge"

def size_order(*, conviction, entry_ceiling, stop, risk_unit_dollars, max_shares=None
               ) -> tuple[int, float]:
    """Conviction-scaled R-based size. risk_unit_dollars is 1R; conviction scales it. shares =
    floor(risk_$/per-share-risk), capped at max_shares. risk_unit_dollars<=0 (unconfigured) ->
    (0, 0.0): the caller renders R-multiples instead of a share count. Never guesses dollars."""
    per_share = entry_ceiling - stop
    if risk_unit_dollars <= 0 or per_share <= 0:
        return 0, 0.0
    budget = risk_unit_dollars * _MULT[conviction]
    shares = int(budget // per_share)
    if max_shares is not None:
        shares = min(shares, max_shares)
    return shares, shares * per_share
```
(Add `_edge_label(v)` = a short `"<dimension>=<bucket> (<tier>, +X.XXR, n=N)"` string. Confirm the `Verdict` field names against reflect.py and adjust.)

**Step 2-4 — TDD:** baseline maps each bucket→grade (confirmed→high, screened→medium, negative-confirmed→avoid, none→medium); sizing math (`risk_unit=$250`, per-share `$2`, high → 125 shares; medium halves; avoid → 0; unconfigured → 0/R-multiple; cap applies; non-positive per-share → 0). Pure + deterministic.

**Step 5 — Commit:** `feat(insight): deterministic baseline conviction + R-based sizing`.

---

## Task 3: The analyst nudge seam (Opus moves the grade, with a reason)

**Files:** Modify `src/swing_screener/notify/analysis.py`; Test `tests/notify/test_analysis_conviction.py`.

**Step 1 — Spec.** Add `analyze_conviction(facts, *, baseline, playbook_text, context_text="", chart_bytes=None, client=None, model="claude-opus-4-8") -> ConvictionResult` (`{conviction, nudge_reason, insight, is_deep}`). Mirror `analyze_signal_deep`'s injectable-client + graceful-fallback pattern. System prompt: the analyst receives the deterministic **baseline** conviction + the playbook + facts + context; it may move the grade **at most ±1** from the baseline (a `_CONVICTIONS`-index clamp in CODE after parsing — never trust the model to respect the bound) and MUST give a one-line reason; it writes the insight (where the pick sits vs the playbook + the external context that bears on the thesis + the single biggest risk). It NEVER changes levels. On ANY failure → `ConvictionResult(conviction=baseline, nudge_reason="(baseline; analyst unavailable)", insight=<deterministic rationale>, is_deep=False)`.

**Step 2-4 — TDD (fake client, no network):** a fake returning `conviction=high, reason=...` when baseline=medium → result high (within ±1); a fake returning `avoid` when baseline=high (a 3-grade jump) → CLAMPED to ±1 (low) in code; a raising/empty fake → baseline + deterministic rationale; the prompt includes the baseline + playbook + the ±1 rule. Clamp is code, not trust.

**Step 5 — Commit:** `feat(analysis): conviction nudge seam (baseline ±1, code-clamped, graceful fallback)`.

---

## Task 4: AnalystCall store + OrderIntent assembly

**Files:** Modify `src/swing_screener/db/models.py` (new `AnalystCall`) + an Alembic migration; `src/swing_screener/pipeline/insight.py` (`build_order_intent` + persist); Test `tests/db/test_analyst_call.py`, `tests/pipeline/test_insight_assemble.py`.

**Step 1 — Model.** `AnalystCall`: `id`, `created_date`, pick keys (`ticker`, `timeframe`, `play_type`, `run_date`), `baseline_conviction`, `final_conviction`, `nudge_reason` (String), `model` (String), and the to-be-scored `realized_r: float|None`, `scored_at: date|None`. Migration: autogenerate against the live schema (nullable columns), follow the repo's alembic runbook (see prior migrations); verify the chain applies on a fresh DB.

**Step 2-4 — Assembly.** `build_order_intent(facts, conviction_result, *, risk_unit_dollars, max_shares) -> OrderIntent` ties the levels (deterministic, from the Signal) + the conviction (final, from the nudge) + `size_order(...)` → an `OrderIntent`. A `record_analyst_call(session, ...)` persists the `AnalystCall` (unscored). TDD: assembly produces the right shares/conviction/levels; the call row is written with both convictions + the reason.

**Step 5 — Commit:** `feat(insight): AnalystCall store + order-intent assembly`.

---

## Task 5: Score analyst calls against shadow outcomes (the learning join)

**Files:** Modify `src/swing_screener/pipeline/run.py` (thread the real `signal_id` into `FillCandidate` — the long-noted None-join fix) + `src/swing_screener/db/repo.py` (a scorer); Test `tests/pipeline/test_analyst_call_scoring.py`.

**Step 1 — The join.** Today `FillCandidate.signal_id` is hardcoded `None` at `run.py` (so `PaperTrade.signal_id` is always null). Thread the persisted `Signal.id` through so a paper trade links to its source signal. Then `score_analyst_calls(session, *, today)` joins each unscored `AnalystCall` to the resolved (closed) baseline/default paper trade for the same pick (via `signal_id`, or the documented `(ticker, timeframe, play_type, run_date)` convention if the FK isn't 1:1), stamping `realized_r` + `scored_at`.

**Step 2-4 — TDD:** an `AnalystCall` whose pick's paper trade closes at +1.5R gets `realized_r=1.5`/`scored_at` set; an unresolved pick stays unscored; the signal_id link is populated on new fills (a regression test that `PaperTrade.signal_id is not None` for a filled signal). Be careful with the prior-bar-detect/next-bar-fill offset — match on the persisted signal_id, not a fragile date guess, once the FK is threaded.

**Step 5 — Commit:** `feat(insight): thread signal_id + score analyst calls against shadow outcomes`.

---

## Task 6: Surface the order intent + feed the analyst's calibration to the reflection

**Files:** Modify `src/swing_screener/notify/run.py` (digest deep path renders the OrderIntent + insight, writes the AnalystCall) + `src/swing_screener/notify/pdf.py`/`body.py` as needed; `src/swing_screener/pipeline/reflect.py` (the reflection reads scored AnalystCalls → the analyst's conviction calibration, surfaced in the edge file); optionally `dashboard/app.py`; Tests across those.

**Step 1 — Surfacing.** In the digest deep path (`notify/run.py:174` region), when deep analysis is on, compute the baseline (from the verdicts sidecar) → `analyze_conviction` → `build_order_intent` → render the conviction + shares + edge + insight in the body/PDF, and `record_analyst_call`. The on-demand ticker path gets the same. Fall back to today's behavior when deep analysis is off or no playbook exists.

**Step 2 — Calibration into the reflection.** `run_reflection` additionally summarizes scored `AnalystCall`s per play type — the analyst's `high`-vs-`low` realized-R and whether its nudges beat the baseline — and the authoring seam surfaces it in the edge file ("Analyst calibration" note). This closes A: the analyst sees its own track record and the playbook records which of its qualitative calls proved out.

**Step 3-4 — TDD:** the digest renders an OrderIntent for a deep pick (mock the analyst); `record_analyst_call` is written; the reflection's calibration summary computes the analyst's hit stats from scored calls. Smoke the digest/dashboard via existing AppTest/mocks; no network.

**Step 5 — Commit:** `feat(insight): surface order intents in the digest + analyst calibration in the reflection`.

---

## Definition of done
- `ruff`/`mypy`/`pytest` green; CI green on the PR.
- Conviction is a deterministic baseline (from the verdicts sidecar) the analyst can move **±1 with a logged reason** (clamp is code); on any LLM failure the baseline holds.
- Every analyst call is persisted and **scored against the pick's shadow outcome**; the reflection surfaces the analyst's own conviction calibration (its judgment earns a track record — North Star #9).
- Sizing is concrete + conviction-scaled off a configured risk unit (R-multiples when unconfigured); **levels are never set here; money never auto-moves** (decision-support; manual execution).
- Analyst calls are joined to their shadow outcome by a **convention join** (`(ticker, timeframe, play_type)` + the earliest fill `opened_date > run_date`), not a `signal_id` FK. *(Resolution: the shadow book fills the re-screened prior-bar signal, which has no persisted DB id, so a 1:1 FK isn't clean. `PaperTrade.signal_id` stays NULL by design; the convention join is robust and tested. Threading a persisted prior-run `signal_id` remains an optional future nicety — see Out of scope.)*

## Out of scope (later phases) / deferred
Execution adapters (manual/paper/robinhood) + autonomy + the kill switch (Phase 3); the analyst proposing its own variants / commissioned tests (Phase 4); auto-widening the ±1 nudge bound from the calibration. **Deferred during build:** the on-demand ticker path (`notify/ondemand.py`) keeps the existing whole-ticker deep analysis (no single signal/levels to grade per pick); threading a persisted prior-run `signal_id` onto shadow fills (the convention join covers the learning loop without it).
