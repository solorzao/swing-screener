# Edge-discovery experiment backlog

2026-06-25 — from a 13-agent research workflow (`edge-discovery-army`) over trading-strategy
families, aggregated into a ranked backlog. Context fed to agents: our diagnosis (continuation
immediate-fade leak), the proven winners (volume thrust, pullback depth, no-flip exit,
reversal CONFIRMED tier), and the tested dead-ends.

## Cross-cutting thesis
The candle is not the edge — the **context** is (supply exhausted? real demand? real trend?
leader vs laggard? favorable regime?). Our reversal edge came from a conviction *subset*; the
continuation analogue is an *intersection* of context filters. **The right way to exploit a
thin edge is to SIZE the good cohort up, not stack more gates** — sources warn >2–3 gates
degrades, matching our `cont_volband` plateau at +0.07R / 95%low<0.

## Run first (cheap, high-value)
1. **regime breakdown diagnostic** (free) — regime already stamped per fill; `breakdown()` by
   market_trend/market_vol × play_type to find where each book's edge concentrates. Gates the
   sign for the regime/RS experiments (our 200DMA split is too coarse — don't assume direction).
2. **volume_dryup_pullback_gate** — pullback bars on drying/below-average volume (supply
   exhausted) then wet resumption. The orthogonal missing half of our volume lever. `_quality_gates_pass`.
3. **pocket_pivot_trigger** — resumption up-bar volume > max down-day volume of prior 10 bars;
   self-normalizing demand>supply, sharper than the fixed 1.3×-mean. A/B vs `cont_volband`.
4. **reversal_volume_sign_test** — `score_reversal:172` rewards HIGH flip-bar volume; test
   whether LOW-volume confirmation (Wyckoff secondary test) ranks better. Affects ranking, not
   the confirmed edge. (A hypothesis, NOT a verified bug — the bar wanting high vs low volume
   is empirical.)

## Full ranked backlog
| # | name | applies | impact | feasibility | one-line |
|---|---|---|---|---|---|
| 1 | regime_breakdown_diagnostic | both | high | free | where does each book's edge live (regime cells)? |
| 2 | volume_dryup_pullback_gate | continuation | high | cheap | dry pullback → wet resumption (full VCP footprint) |
| 3 | pocket_pivot_trigger | continuation | high | cheap | up-vol > worst recent down-vol (sharper thrust) |
| 4 | rs_vs_spy_continuation_gate | continuation | high | new data | pull back in a name OUTperforming SPY (RS line rising) |
| 5 | reversal_volume_sign_test | reversal | high | cheap | flip the high→low volume reward on the confirmed book |
| 6 | delayed_breakeven_arm_1R | exit | med | cheap | don't tighten/trail until ≥1R MFE (stop the scratch tax) |
| 7 | compression_before_trigger | continuation | med | cheap | NR7/VCP coil: narrow contracting pre-trigger range |
| 8 | efficiency_ratio_gate | continuation | med | cheap | Kaufman ER ≥ 0.3 (cheap ADX cousin; chop filter) |
| 9 | regime_routed_book_gates | regime | med | cheap | route continuation→bull, reversal→washout (after #1) |
| 10 | confirmed_reclaim_of_pivot | continuation | med | cheap | require close > pullback high (CONFIRMED-only, anti-chase) |
| 11 | reversal_spring_undercut_reclaim | reversal | med | cheap | Wyckoff spring: undercut support then reclaim (tight stop) |
| 12 | tradelog_feature_mining | both | med | cheap | certify which entry features separate winners (reuses perf.py) |
| 13 | conviction_weighted_vol_sizing | sizing | high | harness | size good tier up / baseline down (no new entry edge) |
| 14 | adx_trend_strength_gate | continuation | med | new indicator | ADX≥25 (only if ER #8 shows signal) |
| 15 | vix_rank_gate_reversal | regime | med | new data | buy bounces only when VIX rank < ~70 (needs ^VIX) |
| 16 | chandelier_trail_conditioned | exit | med | cheap | trail instead of fixed target, only on trending/leader entries |

## Standouts beyond the cheap batch
- **#4 RS-vs-SPY** — most-cited single differentiator, genuinely new (we only ever measured a
  name vs *itself*). One new frame column `close/spy_close`; SPY already fetched for regime.
  Adding a NEW column is harness-legal (`_assert_shared_indicators` only blocks *changing*
  existing indicator periods).
- **#13 conviction sizing** — biggest lever needing no new entry edge; requires per-trade sizing
  in replay/shadow (every fill is risk-equal today — the one real harness gap). Depends on #12.

## Dropped (redundant/dead/untestable)
Re-proposals of tested dead-ends (min_atr_pct, EMA-sep, orderly-shape, RSI>40, MTF-bool,
score-floor); raw outside/engulfing trigger (tested worse); seasonality (decayed); OBV/up-down
volume (redundant with #2/#3); breadth/sector-RS/anchored-VWAP (harness extensions deferred
behind the cheap wins). Full list in the workflow output.

## Real-edge bar (every experiment)
expectancy 95%-low > 0 AND survives `fill_slippage_atr=0.05`, n_closed ≥ 20; prefer gates that
remove the noisiest fades over gates that merely select fewer trades (watch the CI as gates thin
the book). Confirm promising subsets at the FULL universe (bounded baskets flatter — the
`vol_band_body` lesson).

## Results (2026-06-26, branch feat/edge-discovery)
- **#1 regime diagnostic — done.** Reversal edge concentrates in **high market-vol** (+0.30R,
  95%-low +0.078, n=148); ~flat in calm/elevated. Continuation is negative in *every* cell
  (bull/bear × calm/elevated/high) — not regime-rescuable. Reversal is a high-vol play.
- **#15 VIX-rank gate — built, then REFUTED by validation.** Fully implemented + tested
  (`fetch_vix`, `vix_percentile_rank`/`vix_bucket`, `_vix_rank_by_date`, `PaperTrade.vix_bucket`
  + migration, `max_vix_rank` gate). Validation (250 names, reversal CONFIRMED): the gate
  (max_vix_rank=70) **hurts** (+0.15R→+0.07R, 95%-low −0.00→−0.13). Per-bucket, confirmed
  reversals do **best in high VIX** (>70): +0.241R, 95%-low +0.009 (the only positive-significant
  bucket) — the research claim *inverts* on our data (our CONFIRMED+no-flip reversals thrive in
  panic, unlike naive RSI(2) oversold). **Keep `max_vix_rank` OFF**; the `vix_bucket` stamp stays
  useful for attribution. Lesson: validate external research on our own data.
- **#2/#3 volume dry-up + pocket pivot — NEGATIVE.** Continuation (250): dry-up −0.21R (worse
  than default −0.14 — the top NEW idea is a dud on our data), pocket-pivot −0.10R (no help).
  `vol_thrust` stays the only continuation lever; none clears 95%-low > 0.
- **#5 reversal volume-sign — POSITIVE (the wave's win).** Requiring HIGH bounce volume
  (`reversal_min_flip_rvol=1.3`) = **+0.14R, 95%-low +0.06, win 55%, n=823** on the full reversal
  book (best of three; default +0.03, low-vol −0.04). HIGH flip volume is *right* — the "wrong-sign
  bug" was an overclaim. **Confirmed at full 503: +0.183R gross → +0.157R at 0.05 ATR slippage,
  95%-low +0.098, win 56%, n=1,626, 484 clusters** — strengthened with sample; the strongest,
  most robust edge found (beats reversal CONFIRMED's +0.125R net). Shipped as shadow variant
  `rev_highvol`; **promotion candidate** (shadow → surfacing). Use standalone — stacking on
  CONFIRMED-strength thins to n=78 (the two conviction filters overlap).
- **Wave-2 marquee RS-vs-SPY leadership — NEGATIVE on reversal.** `require_rs_leader` (RS line
  above its MA at the bounce; `rs=close/spy` added to build_frame). Validation: flat on the full
  book (+0.03R, same as default; prunes ~75% of trades for no gain) and *hurts* the confirmed book
  (+0.15→+0.08R). "Buy oversold names out-leading SPY" doesn't translate. Third agent-top idea
  refuted (after dry-up and the VIX gate) — pattern: **simple volume conviction wins; sophisticated
  filters don't add.**
- **Wyckoff spring (exp 11) — POSITIVE, a SECOND reversal edge (no-op prediction refuted).**
  `require_spring` (undercut a prior support ≥`spring_gap` bars back, then reclaim). Full 503,
  net 0.05 ATR: **+0.141R, 95%-low +0.059, 427 clusters** (stable from +0.18R/250). Selective
  (the gap forces undercutting a *genuine* prior level, not the immediate bottom).
- **Additivity — volume + structure STACK.** highvol + spring = **+0.25R, 95%-low +0.08,
  win 60%, n=169, 129 clusters** (250, net) vs spring +0.15 / highvol +0.11. The strongest
  reversal config; the two filters catch different quality dimensions.
- **Refined pattern:** structure/volume *conviction* filters DO add for reversal; what failed
  was cross-sectional/regime (RS, VIX) and the entire continuation gate family. Promotion design:
  a TIERED reversal book — surface the premium highvol+spring tier, shadow-track the rest.
