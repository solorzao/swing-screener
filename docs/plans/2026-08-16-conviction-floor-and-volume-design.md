# Conviction floor + volume in deep analysis — design

**Date:** 2026-08-16
**Status:** validated, ready to implement

## Problem

Two asks, one from cost and one from quality:

1. **Cost.** Deep analysis (Opus 5, web search) runs on every surfaced pick — up to 3
   continuation + 3 reversal per daily run. Low-conviction plays are not producing
   results, so the spend on them is waste.
2. **Quality.** Only medium and high conviction plays should reach the screener at all,
   "even if it means we don't have any on some days".
3. **Volume.** Deep analysis must weigh the recent volume that set the play up.

## The structural constraint

The deep-analysis call **is** the conviction call. There is no separate "analysis" step
whose output could be filtered by an already-known conviction:

- `pipeline.insight.conviction_baseline()` computes a free, deterministic grade first.
  It returns only `avoid`, `medium`, or `high` — **never `low`**.
- `notify.analysis.analyze_conviction()` (the paid call) lets the analyst MOVE that
  baseline, clamped in code to ±1 step along `avoid < low < medium < high`. A play type
  that certifies via `analytics.calibration.conviction_calibrated` earns ±2, ceilinged at
  `_NUDGE_CEILING = 2`.
- Therefore `low` is **only** ever produced by an analyst nudging a `medium` down.

So "only deep-analyze medium/high" is circular for the *final* grade. But the *baseline*
is a free pre-call signal, and under the ±1 clamp `baseline=avoid` can only ever land on
`avoid` or `low` — it can never reach `medium`. Those picks are provably un-displayable
under the new rule, so their call is pure waste.

Today nothing filters `avoid`/`low` out of the digest; they print and are merely sized at
0.0R / 0.25R by `insight.size_order`.

## Decisions

| Decision | Choice |
| --- | --- |
| Gate rule | Skip the call only when the baseline **cannot reach** the floor |
| Ungraded picks | Drop, but explain loudly in the digest |
| Scope | Digest body + PDF + proposed orders. Cockpit keeps showing everything |
| Volume role | Analyst-visible only — no gate, no score change |
| Volume facts | Three ratios, reusing the formulas that already exist |

## Part 1 — Gate the spend, filter the surface

### One setting, two uses

Add `min_conviction: str = "medium"` to `StrategyConfig` (config.py), alongside the other
surfacing bars (`reversal_surface_premium_only`, `surface_continuation`). Both the spend
gate and the display filter read the same field so they cannot drift. Setting it to
`"low"` restores today's behavior without a deploy.

`StrategyConfig` (not `Settings`) because this is a strategy surfacing bar, and `scfg` is
already read once per run in `notify.run`.

### The gate

In `_deep_one` and `_batch_deep`, before any model call:

```python
reachable_best = step_up(baseline, nudge_steps.get(play_type, 1))
if _rank(reachable_best) < _rank(scfg.min_conviction):
    skip            # no call, no AnalystCall, no OrderIntent
```

It uses the **same** `max_step` the call itself would pass, so it is self-maintaining: if
a play type ever certifies for ±2, `avoid + 2 = medium` becomes reachable and those picks
automatically resume being analyzed. Nothing to keep in sync with `_NUDGE_CEILING`.

A skipped pick makes no call, records no `AnalystCall`, builds no `OrderIntent` — so it
cannot dispatch and cannot print.

### The filter

After both `_build_picks` calls, drop every pick whose conviction is below the floor **or
absent**, from all three collections:

- `digest_picks` / `reversal_digest` (email body)
- `pdf_picks` / `reversal_pdf` (attachment)
- `collected_intents` (order dispatch)

This must run **before** the dispatch loop (`notify/run.py:801`) and before
`build_proposals`, so a filtered pick never reaches the broker or the shopping list.

Ungraded picks (deep analysis off, playbook missing, spend ceiling hit, beyond top-N) have
`conviction is None` and are dropped by the same rule.

### Empty state

An emptied list stays `[]`, never `None`. The existing convention is that `None` OMITS the
section (continuation parking) while `[]` renders "no setups". A filtered-empty day must
read as an empty day, not as a missing feature.

### The loud line

Modeled on the existing variadic funnel (`notify/body.py:219`), which already renders
stage attribution for reversal:

```
Conviction: 3 graded · 1 skipped (avoid baseline) · 1 below medium · 1 surfaced
```

This is the anti-silence requirement: a quiet market and a broken playbook must not
produce the same empty email. The 2026-07 outage stayed hidden for weeks precisely
because a silently empty digest looked normal.

`repo.save_reversal_funnel` currently records `surfaced=len(reversal_sigs)`, which after
this change is a **pre-filter** count. It must record the post-filter number (or gain a
column) so the persisted funnel does not overstate what was surfaced.

### What this does NOT touch

- The shadow/learning book is booked in `pipeline.shadow`, independent of the digest. It
  keeps booking every signal, so calibration's `low` bucket keeps filling.
- `repo.score_analyst_calls` joins to that shadow book, so scoring is unaffected.
- `conviction_baseline` itself is unchanged.

## Part 2 — Volume in the conviction prompt

### Reuse, don't invent

All three formulas already exist inside `signals/detect.py::_quality_gates_pass` as
**default-off gates** — computed and then discarded:

| Concept | Existing gate | Config |
| --- | --- | --- |
| Thrust | trigger vol ÷ pre-pullback baseline | `vol_thrust_min`, `vol_thrust_excl_pullback` |
| Dry-up | mean pullback vol ÷ pre-pullback baseline | `pullback_vol_dryup_max` |
| Pocket pivot | up trigger vol > worst recent down-day vol | `require_pocket_pivot`, `pocket_pivot_lookback` |

Extract them into pure functions in a new `signals/volume.py`, called from two places:
the existing gates (behavior unchanged) and a new `volume_profile()` that reports the
values. Same code path, so a reported number and a future gate can never disagree.

### The three facts

1. **`rvol_trigger`** — trigger-bar volume ÷ mean volume over `vol_avg_window` bars
   *before* the pullback. Uses the `vol_thrust_excl_pullback` denominator so it is
   directly comparable with dry-up.
2. **`rvol_pullback`** — mean pullback-bar volume ÷ that same baseline. Below 1.0 means
   supply is exhausting (constructive).
3. **`pocket_pivot`** — bool: up trigger bar whose volume exceeds the largest down-day
   volume of the last `pocket_pivot_lookback` bars.

Reversal gets the same three, with the decline window standing in for the pullback.

**Changed during implementation.** The plan was to source reversal's `rvol_trigger` from
the existing `ReversalContext.volume_ratio` so it could not drift from the premium-tier
logic. It is computed with the shared `volume_profile` instead, because `volume_ratio`
uses a `tail(window)` denominator that *includes* the decline it is measured against.
Mixing conventions would have made the two reported numbers incomparable to each other —
"trigger 1.8x vs setup 0.62x" only means something when both share a baseline, and that
comparison is the entire dry-up-then-expansion read.

The trade-off is real and accepted: the reported `rvol_trigger` and the premium tier's
`volume_ratio` can now disagree in magnitude for the same bar. `volume_ratio` keeps its
own arithmetic and the premium tier is untouched, so nothing that gates or sizes changed;
only the analyst's reported fact is new.

### NaN handling differs from the gates — deliberately

The gates **reject** on an unmeasurable ratio (the 2026-07 audit fix: NaN must not
silently no-op a gate). Reporting must not fabricate a number, so `volume_profile` returns
`None` for an unmeasurable term and the prompt omits it. A missing volume read must never
look like a neutral 1.0.

### Plumbing

- `SignalResult` (analyze.py) gains the three optional fields. It already carries `frame`
  and `ctx`, so the profile is computed in `analyze_frames` where `cfg` is in scope.
- `Signal` gains three nullable columns + an alembic migration. Nullable means existing
  rows read as "not measured" rather than as a fabricated neutral.
- `SignalFacts` (notify/analysis.py) gains the three optionals; `_facts()` maps them.
- `_conviction_prompt` renders one line, omitting absent terms:

  ```
  Volume: trigger 1.8x pre-pullback avg · pullback 0.62x (dry-up) · pocket pivot yes
  ```

- `_SYSTEM` gains a sentence: weigh whether recent volume supports the setup —
  contraction through the pullback then expansion on the trigger is constructive, a
  pullback on heavy volume is distribution — and name it in the reason when it moves the
  grade.

### Discipline

Volume informs the analyst's ±1 nudge **only**. It does not gate surfacing, does not move
`conviction_baseline`, and does not enter the ranking score. "Volume dry-up" and "pocket
pivot" are still unrun experiments on the edge-discovery backlog; giving them teeth before
they certify would violate the no-uncertified-edge discipline.

Because every nudge records `nudge_reason` and is later scored against the shadow book,
this starts building the live sample those experiments need, at no extra cost.

## Cost expectations — stated honestly

- The gate saves the Opus call **only** on `baseline=avoid` picks. Its frequency is
  currently **unmeasured**: `local.db` has zero `analyst_calls` rows and prod is Azure
  MSSQL. Query the prod `analyst_calls` table for the `baseline_conviction` distribution
  before promising a dollar figure.
- The Message Batches path (50% off) was checked against prod on 2026-08-16 and is
  **already on** for `daily-digest`, `weekly-digest`, and `monthly-digest`
  (`SWING_DEEP_ANALYSIS_BATCH=1`). No saving is available there.
- `SWING_DEEP_ANALYSIS_TOP_N=3` is live on all digest jobs.
- The filter itself saves nothing directly — it changes what prints. The saving comes
  from the gate, and from not dispatching orders you would not have taken.

## Testing

- `step_up` / rank helpers: pure, table-driven over the full ladder incl. `max_step=2`.
- Gate: `baseline=avoid, max_step=1` → no call; `max_step=2` → call made.
- Filter: low/avoid/ungraded dropped from body, PDF, and intents; medium/high kept.
- Dispatch: a filtered pick never reaches the adapter.
- Empty state: filtered-empty renders "no setups" + the attribution line, not an omitted
  section.
- Volume: extracted functions are byte-equivalent to the current gate arithmetic
  (characterization tests on the existing gates must keep passing); `None` on
  unmeasurable; prompt omits absent terms.
- Migration: round-trips, and old NULL rows read as "not measured".

Run pytest with the `SWING_*` env vars unset — a dev shell with `SWING_BLOB_ACCOUNT_URL`
set turns the blob path on and kills the `test_run.py` tests at import.
