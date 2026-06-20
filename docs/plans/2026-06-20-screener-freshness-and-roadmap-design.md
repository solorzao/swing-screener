# Screener freshness + system roadmap — design

**Date:** 2026-06-20
**Status:** Step 1 shipped (freshness gate + live actionability); roadmap proposed.
**Scope:** Fix the "screeners suggest plays that already ran" complaint, and chart a path
toward a measurement-driven, self-optimizing system (a deterministic
build → deploy → measure → optimize → repeat loop with a strategy leaderboard).

## Problem

The screeners kept surfacing plays that had **already run**. Root causes, all in-code:

1. **No extension guard on the continuation trigger.** `detect_last_bar` fired the instant
   the last bar flipped bullish out of a pullback, with no check on how far that flip candle
   had already traveled from `EMA20`. A tall green HA candle that already ran 2–3 ATR passed
   cleanly.
2. **The score rewarded those extended bars.** `score_signal`'s largest weight (0.35) is
   `body_frac` — the bigger the move that already happened, the higher the rank.
3. **The entry zone permitted chasing** (`ceiling = trigger_close + 0.35·ATR`), and the
   shadow-book fill assumes the worst case at the ceiling.
4. **No live-price actionability gate.** Signals are computed at the trigger close and
   surfaced unchanged later; neither the digest nor the dashboard compared the *current*
   price to `[entry_floor, entry_ceiling]`, so a pick that gapped past its ceiling looked
   identical to a fresh one.
5. **No dedup / staleness** across run-dates (out of scope here; see roadmap).

## Step 1 — shipped

### A. Freshness / anti-chase gate (engine)
- `StrategyConfig.max_extension_atr` (default **2.0**). `0` disables.
- `detect.PullbackContext.extension_atr = (close − EMA20) / ATR`, computed in `detect_last_bar`
  (the detector *reports* the metric; it does not gate — keeps the golden AMD test and the
  direct-detect tests about *structure*).
- `analyze_frames` skips a trigger whose `extension_atr > cfg.max_extension_atr`. The gate
  sits in the pipeline layer so it filters **both** what the screener surfaces and what the
  shadow book forward-tests — the freshness rule becomes part of the strategy end-to-end, so
  the A/B and leaderboard reflect entries we'd actually take.
- **Default chosen empirically:** on the AMD 2018 fixture, fresh in-pullback triggers fire at
  −0.33 and +0.38 ATR of extension; the late chase on 2018-07-26 fires at +2.68. A 2.0 ATR
  threshold keeps the fresh setups and rejects the chase.

### B. Live actionability (surface)
- New pure module `signals/actionability.py`: `classify(entry_floor, entry_ceiling, stop,
  price, buffer_r=0.25) → {status, dist_r}`, where status is `actionable` / `extended` /
  `broken` / `unknown`, and `dist_r` is how far price sits past the ceiling in zone-risk units.
- "Today's Candidates" now quotes each pick's latest close, shows a **Status** column and a
  **Past entry (R)** column, and a default-on **"Hide plays that already ran"** filter. Picks
  with no live quote are kept (fail-open).

### Test strategy
The synthetic `_firing` fixtures use an outsized one-bar green flip on a steep ramp, so their
trigger sits ~5 ATR past `EMA20` — far beyond any realistic chase. Pipeline-mechanics tests
(persistence, ranking, charts, shadow fills, blob, reports) that need that fixture to produce
a signal now pass `StrategyConfig(max_extension_atr=0.0)` to disable the gate, so they keep
testing their actual concern. New tests cover the gate (`test_analyze`), the pure classifier
(`test_actionability`), and the dashboard hide/show behavior (`test_app_smoke`).

## Roadmap — toward the self-optimizing system

The system already has the rare half of the loop: it forward-tests every signal across
parallel exit **arms** with same-sample A/B analytics. To close the loop:

1. **Persist `extension_atr` + `first_seen_date` on `Signal`** (one Alembic migration). Enables
   surfacing "how extended", a staleness/cooldown filter (suppress a setup re-firing within N
   bars, or one that already reached target in the shadow book), and aging of repeats.
2. **Freshness term in the score.** Add an extension/freshness factor so a clean, un-extended
   setup outranks a chased one — counterbalancing the `body_frac` bias.
3. **Screen-level strategy variants as arms.** Today arms vary only the *exit* policy
   (`pipeline/arms.py`). Promote `StrategyConfig` variants (extension thresholds, RSI gates,
   score weights) into named arms forward-tested head-to-head — a true *strategy* leaderboard,
   not just an exit leaderboard.
4. **Leaderboard table + dashboard view** ranking variants by expectancy / profit factor /
   fill rate over a trailing window, *with sample size and significance*.
5. **Score calibration.** Track realized expectancy by score decile to verify the score
   predicts winners; a flat curve means the score is miscalibrated.
6. **Regime tagging.** Stamp each run with market context (SPY vs 200DMA, volatility bucket)
   and break performance down by regime — continuation wants uptrends, reversals want washouts.
7. **Scheduled optimizer/sweep job** that replays history across a config grid with
   walk-forward / out-of-sample guardrails and proposes the next config — the deterministic
   analog of the post's "AI-native orchestrator."

Smaller features: earnings-date avoidance, liquidity/gap gating, sector-breadth context in the
digest, R-based position sizing, and intraday **entry alerts** (notify when a candidate trades
into its zone — the inverse of the exit alert).
