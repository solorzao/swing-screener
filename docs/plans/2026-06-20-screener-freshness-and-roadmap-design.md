# Screener freshness + system roadmap — design

**Date:** 2026-06-20
**Status:** Steps 1–8 shipped (freshness gate, live actionability, persisted
extension/first-seen, freshness score term, staleness cooldown, screen-variant shadow-book
dimension + strategy leaderboard, offline replay/backtest harness, leaderboard significance,
score calibration, market-regime attribution, config-sweep optimizer). The measurement loop is
complete; remaining work is ops + smaller features.
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

## Step 2 — shipped

- **Persisted `extension_atr` + `first_seen_date` on `Signal`** (migration
  `e9c7b3a15d24`, both nullable). `SignalResult.extension_atr` carries the metric through the
  pipeline; reversals and legacy rows are NULL.
- **`first_seen_date` = streak start.** `repo.prior_first_seen` maps the immediately-prior
  run's `(ticker, timeframe, play_type)` setups to their first-seen date; the pipeline inherits
  it when the same setup fires again, else stamps today. The lookup only considers
  `run_date < today`, so a same-day re-run is still idempotent.
- **Freshness term in the score** (`score.py`/`build_score.py`): `freshness = clip01(1 -
  extension/max_extension_atr)`, weighted **0.10**, carved out of trigger `strength` (0.35 →
  0.25) which over-rewarded big already-run candles. Clean setups now outrank chases.
- **Staleness cooldown.** `notify/select.py` pickers take `max_age_days`; `send_digest` passes
  `StrategyConfig.digest_repeat_cooldown_days` (default **1**) so a setup on the list longer
  than the window stops being re-pitched. The dashboard "Today's Candidates" gains **Ext (ATR)**
  + **First seen** columns and a default-on "Hide repeats" filter. NULL-first_seen rows always
  pass (fail-open), so legacy rows and existing tests are unaffected.

## Step 3 — shipped (live screen-variant dimension)

A second, orthogonal experiment dimension on the shadow book. `arm` varies the EXIT policy on
a shared fill (same-sample); the new **`variant`** varies the ENTRY/screen config.

- **`pipeline/variants.py`** — `build_screen_variants(base)` returns `{name: StrategyConfig}`;
  the shipped set is `default` (the live config) + `extguard_tight` (freshness gate 1.5 vs the
  base's 2.0). Variants MUST share the base's indicator periods (the shadow book reuses the
  base frames); a guard raises if one retunes an indicator.
- **Pipeline** (`pipeline/run.py`) re-screens the prior bar under each alt variant (reusing the
  enriched frames) and books each variant's own fills under the baseline exit only — one extra
  book per variant. `advance_open` keys exits off `arm`, so variant trades ride the baseline
  exit with no downstream changes.
- **Data model** — `paper_trades.variant` (migration `c4d8e2f6a9b1`, NOT NULL server_default
  `default`, indexed). `breakdown(baseline-arm trades, "variant")` is the leaderboard.
- **Dashboard** — "Screener Performance" leads with a **Strategy leaderboard** (variants ranked
  by expectancy under the baseline exit); the exit-arm A/B and headline metric are scoped to the
  `default` variant so they stay honest.
- Per the chosen "both, live first" plan, the **offline replay/backtest harness** (sweep many
  configs over history, emit a leaderboard) is the next step — it reuses this `variant` plumbing.

## Step 4 — shipped (offline replay/backtest harness)

`pipeline/replay.py` — the offline complement to the live shadow book. `replay(frames, *,
timeframe, base_cfg, variants)` walks each ticker's historical OHLCV forward bar-by-bar,
driving the SAME `open_from_signals` + `advance_open` against a throwaway SQLite db (so the
partial/trail exit machinery is reproduced exactly, not re-implemented), then returns
`breakdown(trades, "variant")`. A `python -m swing_screener.pipeline.replay --tickers AMD,NVDA`
CLI replays the cached daily history and prints a leaderboard. No network — feed it the parquet
cache or a CSV. Assumes one bar per calendar day (1d/1wk/1mo); 4h needs per-day batching (the
same-day guard in `advance_open`), noted as a follow-up.

## Step 5 — shipped (leaderboard significance)

`PerformanceSummary` now carries `expectancy_stderr` + a 95% CI (`expectancy_ci_low/high`,
normal approximation; sample stdev needs n ≥ 2, so a thinner sample's interval collapses to the
point estimate). `analytics.performance.MIN_LEADERBOARD_N` (20) is the trust threshold. Both
leaderboards — the dashboard and the replay CLI — now rank **trusted samples (n ≥ threshold)
above thin ones, then by the lower CI bound**, so a lone lucky trade can't top a deep, steady
variant (its 1-sample CI collapses to the point estimate, which the two-tier key defeats). The
dashboard shows the 95% interval + a thin/ok flag and adds a trailing-window cut (90/180/365d by
open date).

## Step 6 — shipped (score calibration)

`analytics.performance.score_bucket(trades, edges)` buckets closed trades by `signal_score`
into bands and summarizes each, so the dashboard can ask: does a higher composite score
actually earn more? "Screener Performance" now renders a **Score calibration** section — an
expectancy-by-band bar chart + table (bands `0.0–0.5 / 0.5–0.6 / 0.6–0.7 / 0.7–0.8 / 0.8–1.0`,
empty bands hidden from the chart) — with a caption: a predictive score trends up across bands;
a flat or inverted curve means the weights need rework. Runs on the selected variant/arm, so
calibration can be read per screen config.

## Step 7 — shipped (market-regime attribution)

`pipeline/regime.py` — `classify_regime(spy_daily, cfg)` reads the SPY daily proxy and returns
a `MarketRegime(trend, vol)`: trend `bull`/`bear` (last close vs the 200-day SMA), vol
`calm`/`elevated`/`high` (ATR% bands). The pipeline classifies once per run — routed through the
same fetch seam as the universe so tests stay offline, and **skipped entirely on a no-fill run**
(no fills, no SPY fetch) — and stamps every paper trade with `market_trend` + `market_vol`
(migration `d5b9f3a72e16`, nullable; NULL = unknown / SPY unavailable / legacy). "Screener
Performance" gains a **Performance by market regime** cut (expectancy-by-trend chart + a
trend/vol table), so you can see whether continuation really wants bull regimes and reversals
the washouts. Unknown-regime trades are excluded from the cut.

## Step 8 — shipped (config-sweep optimizer)

`pipeline/optimize.py` — the capstone that turns the measurement stack into the post's
build → measure → optimize → repeat loop. `optimize(frames, *, timeframe, grid, oos_frac)`
drives the replay harness over a config grid (a grid is just a variant set, so it reuses
`replay`), with a **walk-forward guard**: it ranks the grid on an in-sample (earlier) slice
using the shared `leaderboard_order` (trust-tiered, significance-aware), then reports the
winner's out-of-sample (later) performance so an edge that only fits the past is exposed.
`build_config_grid` sweeps the freshness gate (`max_extension_atr` ∈ 1.0/1.5/2.0/2.5, the
incumbent included). `format_report` prints the in-sample leaderboard, the winner, and a
promote / keep-current verdict. CLI: `python -m swing_screener.pipeline.optimize --tickers
AMD,NVDA`. Offline + deterministic, no DB writes.

Refactor: the trust-tiered ranking moved to `analytics.performance.leaderboard_order`, now
shared by the dashboard leaderboard, the replay CLI, and the optimizer (one source of truth).

## Roadmap — complete; remaining work is ops + smaller features

The full self-optimizing loop is in place: live forward-testing (exit arms + screen variants),
the offline replay harness, trustworthy significance-ranked leaderboards, score calibration,
regime attribution, and the config-sweep optimizer that proposes the next variant set. What
remains is operational, not architectural:

- **Schedule the optimizer** as a seventh Azure Container Apps Job (mirrors the existing six +
  the deploy runbook) so it sweeps on a cadence and surfaces a proposal — the only piece of the
  "scheduled" optimizer that's deployment rather than code.
- Promote a proven variant from the leaderboard/optimizer into `build_screen_variants`.
- Extend the optimizer grid beyond the freshness gate (e.g. RSI gates, score weights) and the
  replay harness to 4h (per-day batching for `advance_open`'s same-day guard).
- Smaller features: earnings-date avoidance, liquidity/gap gating, sector-breadth context in the
  digest, R-based position sizing, and intraday **entry alerts** (notify when a candidate trades
  into its zone — the inverse of the exit alert).

Smaller features: earnings-date avoidance, liquidity/gap gating, sector-breadth context in the
digest, R-based position sizing, and intraday **entry alerts** (notify when a candidate trades
into its zone — the inverse of the exit alert).
