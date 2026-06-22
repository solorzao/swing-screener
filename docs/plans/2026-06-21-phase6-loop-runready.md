# Phase 6 — deepen the learning loop + run-readiness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Finish the buildable-now work so the only remaining human jobs are debug / monitor / feed-the-data — the analyst proposes its own screen variants + earns a wider (calibration-gated) conviction nudge, and the insight engine is safe to run production-on with a health surface. **Nothing moves money** (default execution stays `off`; levels + the money boundary untouched).

**Architecture:** Five existing seams do the heavy lifting — `replay`/`optimize` already consume an arbitrary config grid; `conviction_calibrated` is already the autonomy gate's trusted test; the Opus call already returns `resp.usage`; the dashboard health primitives exist read-only. Phase 6 wires them: a validated `ProposedVariant` store the optimizer sweeps (count fed into the multiple-comparisons control), a calibration-gated `max_step` on the nudge clamp, token-spend logging + a per-run abort ceiling, and a System Health page + digest line.

**Tech Stack:** Python 3.12, SQLAlchemy + Alembic, anthropic (mocked in tests), Streamlit, pytest. No new deps.

**Governed by:** [North Star](../NORTH_STAR.md) (#1/#2/#8/#9) + the [Phase-6 design](2026-06-21-phase6-loop-runready-design.md). Builds on Phase 0-5.

---

## Invariants (every task)
- **Nothing moves money / no autonomy.** Default `execution_mode=off`; the analyst only *queues* variants (promotion stays the human-gated `propose()` gate) and only *earns* a bounded ±2 sizing nudge; levels stay deterministic.
- **Defaults preserve today exactly.** `max_step=1`, the cost ceiling unset, no proposed variants → byte-identical behavior; every existing test passes.
- **Honest evidence.** A wider search (analyst variants) is paid for in the multiple-comparisons control; the nudge widening is earned + reversible (recomputed each run).
- **Commit discipline:** gate on green (`ruff check src tests alembic`, `mypy`, `pytest -q`) before each commit. No `--no-verify`. Trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Migrations → single `alembic heads`.

---

## Task 1: Opus token-spend logging (B1a — no behavior change)

**Files:** `notify/analysis.py` (capture `resp.usage` → the result dataclasses); `pipeline/insight.py` + `db/models.py` (`AnalystCall` cost columns) + a migration; `notify/run.py` (`record_analyst_call` threads it); Tests.

**Step 1 — Capture usage.** Add a frozen `Usage{input_tokens: int, output_tokens: int, web_searches: int, est_cost_usd: float}` (in analysis.py). `_create_message` returns the raw `resp` whose `.usage` carries token counts; surface a `Usage` on `SignalAnalysis` (line ~78) and `ConvictionResult` (line ~520) as `usage: Usage | None = None`. Estimate `est_cost_usd` from a small `_MODEL_PRICES` table (input/output per-MTok for `claude-opus-4-8`, + a per-web-search cost) — **consult the claude-api skill for current Opus 4.8 pricing**; the estimate is for a safety cap, approximate is fine. The fallback/deterministic paths set `usage=None`.

**Step 2 — Persist on `AnalystCall`.** Add nullable `input_tokens: int|None`, `output_tokens: int|None`, `web_searches: int|None`, `est_cost_usd: float|None` to `AnalystCall` (`db/models.py`) + a migration (server_default NULL). `record_analyst_call` (insight.py / run.py) writes them from the `ConvictionResult.usage`.

**Step 3 — TDD.** A fake client returning a `usage` (input/output tokens) → `analyze_conviction` surfaces a `Usage` with the right tokens + a positive `est_cost_usd`; `record_analyst_call` persists them; the deterministic-fallback path → `usage=None`, cost columns NULL. Migration single-head. **No behavior change** (existing analysis/digest tests stay green).

**Step 4 — Commit:** `feat(cost): capture + persist Opus token spend on AnalystCall`.

---

## Task 2: Per-run spend ceiling (B1b — fail-safe)

**Files:** `settings.py` (`SWING_DEEP_ANALYSIS_MAX_USD`); `notify/run.py` (the deep loop); Tests.

**Step 1 — Setting.** `deep_analysis_max_usd: float | None` (env `SWING_DEEP_ANALYSIS_MAX_USD`, via `_opt_float`, **default None = no ceiling** = today's behavior).

**Step 2 — The ceiling.** In `send_digest`'s deep-analysis loop (`_build_picks` / `_deep_one`, run.py ~424-460), accumulate `est_cost_usd` across deep calls **for the run**. Before each `_deep_one`, if the accumulator has reached `deep_analysis_max_usd`, **skip the deep analyst and fall back to the deterministic `analyze_signal`** for the remaining picks (log the cutoff once: "deep-analysis spend ceiling $X reached; remaining picks use deterministic text"). The intent/ticket still renders from the deterministic path. None ceiling → never triggers.

**Step 3 — TDD.** With a fake analyst whose `Usage.est_cost_usd` sums past a small `SWING_DEEP_ANALYSIS_MAX_USD`, the digest runs the deep analyst until the ceiling, then the rest fall back to deterministic (assert the fake deep fn is called only N times + the later picks are deterministic + the cutoff logged); None ceiling → all picks deep (regression). Reuse the digest harness.

**Step 4 — Commit:** `feat(cost): per-run deep-analysis spend ceiling (abort to deterministic, fail-safe)`.

---

## Task 3: The calibration-gated nudge bound (A2)

**Files:** `notify/analysis.py` (`max_step` param + clamp); `analytics/calibration.py` or `notify/analysis.py` (`max_conviction_step`); `notify/run.py` (wire per-play-type); Tests.

**Step 1 — Parameterize the clamp.** `_clamp_conviction(parsed, baseline, *, max_step: int = 1)` (analysis.py:562): clamp the index to `[base_idx - max_step, base_idx + max_step]`. Thread `max_step` through `_parse_conviction` (575) and `analyze_conviction` (594, add `max_step: int = 1`). **Default 1 → byte-identical; all existing nudge tests pass.**

**Step 2 — The earned-bound (pure).** `max_conviction_step(calib: CalibrationVerdict, *, ceiling: int = 2) -> int`: return `min(ceiling, 2)` when `calib.calibrated` is True, else `1`. (Named `ceiling=2` constant — the git-visible, bounded discipline.) Place it next to `conviction_calibrated` (calibration.py) or in insight.py.

**Step 3 — Wire it per play type.** In the digest deep path (run.py — where the playbook/calibration is loaded per play type, ~365/391), compute `calib = conviction_calibrated(load_scored_analyst_calls(session, play_type=pt))` (or reuse the value the gate already computes if available), then `max_step = max_conviction_step(calib)`, and pass `max_step=max_step` into `analyze_conviction`. (Continuation may earn ±2 while reversal stays ±1.) Recomputed every run → reversible.

**Step 4 — TDD.** `_clamp_conviction`: `max_step=1` clamps ±1 (byte-identical); `max_step=2` permits ±2 but still bounds (a 3-step jump → clamped to ±2). `max_conviction_step`: `calibrated=True` → 2; `False` → 1; never exceeds `ceiling`. A digest test: a calibrated play type lets a ±2 nudge through; an uncalibrated one clamps to ±1. The analyst still can't move a level (unchanged).

**Step 5 — Commit:** `feat(insight): calibration-gated conviction-nudge bound (earned, capped at +-2, per play type)`.

---

## Task 4: System Health surface (B2)

**Files:** `dashboard/app.py` (`_render_health` + `PAGES`); `notify/run.py` + `notify/body.py` (a digest health line); `db/repo.py` (a tiny health-summary helper if cleaner); Tests.

**Step 1 — The dashboard page.** `_render_health(session)` registered in `PAGES` (app.py ~987): (i) a per-job last-run table — for each scheduled kind/job, the latest run/email from `repo.latest_run_date` + `repo.list_email_log`, with a **red/green stale badge** (e.g. screen older than ~36h = red); (ii) the **gate countdown** (`autonomy_gate(session)` + `gate_countdown` / `render_report`); (iii) an **execution-mode** chip (`load_settings().execution_mode`); (iv) optionally the day's deep-analysis spend (sum the cost columns from Task 1). Read-only; degrades gracefully on empty data.

**Step 2 — The digest health line (push).** A one-line footer in the daily digest body (text + HTML): e.g. `"Health: last screen <date> (fresh) · execution off · gate NOT READY"`. Reuses the existing footer/gate-status-line plumbing. Gate it so it's additive (always-on one-liner is fine; it's read-only + cheap).

**Step 3 — TDD.** AppTest (or the dashboard smoke pattern) renders the System Health page with a seeded run/email + asserts the stale badge logic (fresh vs stale) + the gate line + the mode chip. A digest test asserts the health footer line appears with the right freshness/mode. Read-only (no writes).

**Step 4 — Commit:** `feat(ops): System Health dashboard page + digest health line`.

---

## Task 5: ProposedVariant store + validation + grid merge + MC accounting (A1 — data layer, NO LLM yet)

**Files:** Create `pipeline/proposed.py` (the schema + store + `to_config`); `pipeline/variants.py` / `pipeline/optimize.py` (merge queued variants into the grid + the multiple-comparisons count); Tests.

**Step 1 — The schema + store.** A frozen `ProposedVariant{name, play_type, delta: dict[str, float|int|str], rationale, hunch_ref, status, drafted_at, provenance}` + `proposed_to_json`/`load_proposed` (mirror `reflect.verdicts_to_json`/`load_verdicts`) persisted as `edge/<pt>.proposed.json` (gitignored like the verdicts sidecar, or committed — match the verdicts convention; document). `to_config(pv, base: StrategyConfig) -> StrategyConfig`: `dataclasses.replace(base, **pv.delta)` then `variants._assert_shared_indicators(base, result)` → **raise on an illegal delta** (touches a frozen indicator field) or an unknown key. Pure + validated.

**Step 2 — Merge into the sweep + count the search.** Extend `build_screen_variants` / `build_config_grid` (optimize.py) to merge `status="queued"` `ProposedVariant`s (via `to_config`) into the grid. **Multiple-comparisons honesty (the load-bearing bit):** AUDIT how `optimize.py` / `propose.py` currently control for the number of variants compared when selecting a winner. The count of swept variants (including analyst-queued) MUST feed that control — if a correction exists, include analyst variants in its denominator; if none exists, add a grid-size-aware guard (or at minimum surface the grid size into the `propose()` provenance so the winner can't be cherry-picked from a silently-widened search). Document exactly what you found + did. (Do NOT loosen the existing single-gate teeth.)

**Step 3 — TDD.** `to_config`: a legal delta (e.g. a target/pullback knob) → the replaced config; an illegal delta (a frozen indicator field) → raises; an unknown key → raises. Round-trip the store JSON. The grid merge includes a queued variant + excludes a non-queued one; the MC count reflects the added variants (assert the denominator/provenance grows with queued variants). Pure; no LLM, no network.

**Step 4 — Commit:** `feat(loop): ProposedVariant store + validated to_config + optimizer grid merge (search-cost accounted)`.

---

## Task 6: The reflection drafting seam (A1 — the LLM, into the validated store)

**Files:** `pipeline/reflect.py` (the author seam drafts a candidate on a hunch); Tests.

**Step 1 — Draft on a hunch.** In the reflection (`run_reflection` / `author_edge_file`, reflect.py ~624/748), for a `tier="hunch"` ("needs a test") verdict, let the Opus author additionally emit a **structured** candidate `ProposedVariant` (a `delta` keyed to the hunch's dimension/bucket + a one-line rationale + `hunch_ref`). Validate it through `to_config` (Task 5) — **an invalid/malformed draft is dropped with a warning, never persisted/swept** — and write the valid ones (`status="queued"`) to `edge/<pt>.proposed.json`. Code owns the schema/validation/frontmatter; the LLM only proposes. Deterministic fallback: no draft (the store is simply not updated). The store rides the existing human-gated reflection PR (`reflect.yml` already writes `edge/*` — extend its add-paths to `edge/*.proposed.json`).

**Step 2 — TDD (mock the author).** A fake author returning a valid candidate delta → a `queued` `ProposedVariant` is written to the store (parseable, legal); a fake returning an ILLEGAL/malformed delta → dropped (not written), warned, the run doesn't fail; a deterministic/blank author → no draft, no store change. The drafted variant then merges into the grid (Task 5) — an integration assert. No network.

**Step 3 — Commit:** `feat(loop): reflection drafts validated candidate variants on a needs-a-test hunch`.

---

## Definition of done
- `ruff`/`mypy`/`pytest` green; CI green; single `alembic heads`.
- **Run-readiness:** Opus token spend is logged on `AnalystCall`; a per-run `SWING_DEEP_ANALYSIS_MAX_USD` ceiling falls back to deterministic text (fail-safe); a System Health page + a digest health line make "is it healthy + accruing + how close is the gate" legible. Turning `SWING_DEEP_ANALYSIS` on in prod is now safe + observable.
- **Learning loop:** the analyst's conviction nudge earns ±2 only where its calibration certifies (capped, per-play-type, reversible); the analyst drafts its own validated screen variants that queue into the optimizer (the widened search paid for in the MC control) — promotion still human-gated.
- **Boundaries intact:** nothing moves money; levels deterministic; defaults byte-identical; an illegal proposed delta never reaches the grid.

## Out of scope (later)
Multi-knob auto-promotion in `propose()`; the nudge-adds-R inferential test; a `RunLog` error feed; a cheaper routine model; the execution polish + the real-money arming flip (a human act once the gate passes). Auto-arming is never in scope.
