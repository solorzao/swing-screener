# 2026-07-25 free-diagnostics results (queue Part A: Q1-Q5)

Runs of the five pre-registered free diagnostics from
[2026-07-25-strategy-review-experiment-queue.md](2026-07-25-strategy-review-experiment-queue.md),
all on the pinned 511-name corpus (as-of 20260703, 0.05 ATR slippage baked into realized_r,
ticker-clustered 95% CIs). Every verdict below was independently verified: a second agent
re-derived the headline numbers from the raw parquets with fresh code and reproduced them
exactly before the verdict was accepted. Scripts: replay_rev_crosstab.py,
replay_early_highvol.py, replay_cont_medvol_gates.py, replay_rotation_tag.py,
replay_tier_ladder.py (run from the repo root with the project venv python; the bare system
python lacks mplfinance, which pipeline.replay pulls in transitively).

## Verdict summary

| Q | diagnostic | verdict |
|---|---|---|
| Q1 | bear x high-vol cross-tab | NEITHER pre-registered pattern: BEAR is the load-bearing axis (bear x not-high lb +0.080), high-vol is an amplifier INSIDE bear (bear x high lb +0.218, Bonferroni-6 +0.187) and carries nothing in bull (bull x high lb -0.055). Strongest promotion cohort = bear x high. |
| Q2 | EARLY x high-vol coherence (cell A) | PASS: +0.171R, clustered lb +0.109, n=1,940, 219 clusters, half-width 0.063 <= 0.10. Cell B + 0.10-slippage re-runs still required for CONFIRMED-COHERENT. |
| Q3 | continuation med-vol strict gates | PARK: 0 of 6 pre-registered cells clears (best: -0.044R, corrected lb -0.151, vs baseline -0.150R). Closes the last selection-side continuation open questions (shallow-pullback bullet closed with a med-vol-slice scope caveat; ma_rising was degenerate - 100% true - implied by the detector trend precondition). |
| Q4 | sector-rotation cluster-RS tag | RETIRE the rotation thread: the tag discriminates (41.0% tagged vs legacy 81.5%) but has ZERO edge (delta +0.0004, clustered lb -0.029, n_tagged 17,187/501 clusters; 0.10-slippage direction also fails). Pre-registered: no gate, no ranking dimension. |
| Q5 | conviction-tier ladder certification | NOT CERTIFIED (both ladders): stamped premium-vs-rest delta +0.050R corrected lb -0.043; lag-aware challenger +0.042R lb -0.064. Conviction-weighted sizing (Q8) stays BLOCKED; conviction_tier demotes to a surfacing label. |

## Design implications

- The reversal thesis sharpens to "this play wants a BEAR tape": high-vol standalone was
  riding its bear overlap (bull x high is negative), which resolves the volatility sign-flip
  open question. The Q9 premium-tier redefinition (highvol-only) must be reconsidered - an
  unconditioned highvol tier would surface in bull, where the cohort carries nothing; the
  promotion cohort the evidence points at is bear x high (lb +0.218), with EARLY x high-vol
  (Q2) suggesting the EARLY cohort need not be excluded in that context.
- Continuation selection-side is now fully closed (Q3). The remaining continuation item is
  the Q6 ceiling sweep (entry economics); a null there completes the parking case.
- Q8 (sizing) is blocked on a certified ladder (Q5): neither current premium nor the
  lag-aware challenger separates. The closest miss was confirmed-or-highvol vs rest
  (corrected lb -0.0075) - a cohort statement, not a ladder.

---


# Appendix Q1 - full run report

# Q1 rev_bear_highvol_crosstab — are the bear-regime and high-vol reversal screens the same trades?

- Script: `scripts/replay_rev_crosstab.py`
- Run: `cd <worktree> && PYTHONPATH=src python scripts/replay_rev_crosstab.py --cache-root "C:/Users/Oliver/source/repos/swing-screener/.cache"`
- Input: pinned D_dump shards s0–s7 at slip 0.05 (slippage already baked into `realized_r`), `play_type == "reversal"` only: 70,277 rows, 511 tickers, single variant `default`.
- Bounds: house `ci_low` = ticker-clustered bootstrap 2.5th percentile (min-clamped to the IID bound, seed 12345, 1000 draws). `lb_bonf6` = the same house bootstrap at the alpha/6-corrected 0.4167th percentile (min-clamped to the IID bound at z=2.638), applied to the K=6 trend×tier cell family (each of the FULL and CONFIRMED tables treated as its own K=6 family). Grading = closed filled trades only (`breakdown` grades `status=="closed" & fill_status=="filled"`). fill% = filled/(filled+missed).
- Eligibility to interpret: n_closed >= 20 and clusters >= 8. Every non-empty cell below is eligible.

These are INTERNAL anchors on the pinned 511-name corpus (as-of 20260703); they intentionally differ from edge/reversal.md's unpinned ~100-ticker numbers — compare only within this file.

## (a) FULL book — marginals

| cohort | exp R | ci_low | n_closed | clusters | fill% |
|---|---|---|---|---|---|
| trend=bull | -0.010 | -0.029 | 27446 | 511 | 64% |
| trend=bear | +0.132 | +0.104 | 11791 | 511 | 67% |
| trend=None | -0.040 | -0.081 | 5056 | 493 | 61% |
| tier=low | +0.004 | -0.033 | 9984 | 379 | 66% |
| tier=med | +0.021 | +0.003 | 31962 | 505 | 64% |
| tier=high | +0.159 | +0.093 | 2347 | 219 | 63% |

## (b) FULL book — market_trend × volatility_tier (K=6 Bonferroni family)

| cohort | exp R | ci_low | lb_bonf6 | n_closed | clusters | fill% |
|---|---|---|---|---|---|---|
| bull × low | -0.001 | -0.040 | -0.051 | 8517 | 376 | 66% |
| bull × med | -0.017 | -0.042 | -0.051 | 18135 | 501 | 64% |
| bull × high | +0.056 | -0.055 | -0.088 | 794 | 112 | 61% |
| bear × low | -0.129 | -0.237 | -0.270 | 718 | 179 | 72% |
| bear × med | +0.130 | +0.098 | +0.092 | 9848 | 500 | 67% |
| bear × high | +0.299 | +0.218 | +0.187 | 1225 | 206 | 65% |
| bear × not-high (pooled) | +0.112 | +0.080 | — | 10566 | 505 | 67% |

Cells clearing lb > 0 raw AND with the alpha/6-corrected bound: **bear×med** (+0.098 / +0.092) and **bear×high** (+0.218 / +0.187). No other cell clears even raw.

## (c) FULL book — bear × high split by market_vol

| cohort | exp R | ci_low | n_closed | clusters | fill% |
|---|---|---|---|---|---|
| bear × high × calm | — | — | 0 | 0 | — |
| bear × high × elevated | +0.144 | -0.030 | 335 | 83 | 71% |
| bear × high × high | +0.357 | +0.270 | 890 | 200 | 63% |

The bear×high cell's edge is concentrated in market_vol=high (lb +0.270); the elevated slice does not clear on its own. The calm slice is empty (bear regime + high volatility_tier never coincides with a calm market_vol stamp in this corpus).

## (d) CONFIRMED cohort (strength=="confirmed") — same tables

Marginals:

| cohort | exp R | ci_low | n_closed | clusters | fill% |
|---|---|---|---|---|---|
| trend=bull | +0.063 | +0.029 | 5933 | 511 | 42% |
| trend=bear | +0.013 | -0.037 | 2800 | 502 | 48% |
| trend=None | -0.034 | -0.113 | 973 | 427 | 37% |
| tier=low | +0.024 | -0.032 | 2401 | 345 | 46% |
| tier=med | +0.040 | +0.009 | 6898 | 503 | 43% |
| tier=high | +0.101 | -0.023 | 407 | 113 | 38% |

Cells (own K=6 family):

| cohort | exp R | ci_low | lb_bonf6 | n_closed | clusters | fill% |
|---|---|---|---|---|---|---|
| bull × low | +0.039 | -0.024 | -0.042 | 2044 | 340 | 46% |
| bull × med | +0.076 | +0.034 | +0.019 | 3751 | 489 | 41% |
| bull × high | +0.045 | -0.191 | -0.267 | 138 | 60 | 36% |
| bear × low | -0.412 | -0.583 | -0.657 | 188 | 112 | 57% |
| bear × med | +0.027 | -0.027 | -0.046 | 2408 | 479 | 49% |
| bear × high | +0.236 | +0.057 | -0.003 | 204 | 77 | 39% |
| bear × not-high (pooled) | -0.004 | -0.058 | — | 2596 | 486 | 49% |

Raw clears: bull×med (+0.034) and bear×high (+0.057). Corrected clears: **bull×med only** (+0.019); bear×high fails the corrected bound by a hair (-0.003).

bear × high split by market_vol (CONFIRMED):

| cohort | exp R | ci_low | n_closed | clusters | fill% |
|---|---|---|---|---|---|
| bear × high × calm | — | — | 0 | 0 | — |
| bear × high × elevated | +0.036 | -0.383 | 46 | 30 | 38% |
| bear × high × high | +0.294 | +0.104 | 158 | 71 | 39% |

## Pre-registered decision rule (restated)

"One edge counted twice" is CONFIRMED if bear-AND-high's clustered lb > 0 while BOTH off-diagonals (bear-AND-not-high pooled; bull-AND-high) fail lb > 0. Labels are ADDITIVE if bear-AND-not-high and bull-AND-high independently clear. Also report which label (bear, high, or the intersection) is the strongest single cohort definition for the premium-tier promotion design. Interpret only cells with n_closed >= 20 and clusters >= 8.

## Verdict

**FULL book (primary): NEITHER pre-registered pattern holds cleanly — the labels are PARTIALLY redundant, with bear as the load-bearing axis.**

- bear×high: lb = +0.218 (n=1225, cl=206) → clears.
- bear×not-high pooled: lb = +0.080 (n=10566, cl=505) → ALSO clears (driven entirely by bear×med; bear×low is negative), so it is NOT "one edge counted twice" — bear carries an edge outside the high-vol tier.
- bull×high: lb = -0.055 (n=794, cl=112) → fails, so the labels are NOT additive — high-vol has no measurable edge outside bear.

Reading: "high volatility_tier" is not an independent screen — it is an amplifier inside the bear regime. "Bear" is the real edge axis; high-vol selects its best slice.

**CONFIRMED cohort (secondary): the pre-registered "ONE EDGE COUNTED TWICE" pattern triggers on raw bounds** (bear×high lb +0.057 clears; bear×not-high -0.058 and bull×high -0.191 both fail), but bear×high does not survive the alpha/6 correction (-0.003), so treat the confirmed-cohort intersection claim as suggestive, not certified.

**Strongest single cohort for premium-tier promotion: the intersection (bear × high), lb +0.218** vs bear marginal +0.104 vs high-tier marginal +0.093 (FULL book). The third axis sharpens it further: bear × high × market_vol=high has lb +0.270 (n=890, cl=200) — the strongest eligible cohort in the study — but that is a post-hoc third cut outside the pre-registered K=6 family, so use it as design input, not as a certified bound.

## Caveats

- Pinned 511-name corpus as-of 20260703; marginals intentionally differ from edge/reversal.md's unpinned ~100-ticker anchors. All comparisons here are internal to this file.
- 8,478 reversal rows carry market_trend=None (no SPY context stamp); they appear in the trend marginals but are excluded from the trend×tier cell family by construction.
- The Bonferroni-corrected bound reads the 0.4167th percentile of 1,000 bootstrap draws — between the 4th and 5th smallest draw, so tail resolution is thin; it is min-clamped to the corrected-z IID bound per house semantics, which mitigates but does not remove that. The confirmed-cohort bear×high corrected miss (-0.003) is well within that resolution — a coin-flip call, hence "suggestive".
- FULL and CONFIRMED cell tables were each corrected as their own K=6 family; correcting over the combined K=12 would push the confirmed bull×med clear (+0.019) toward the line as well.
- The market_vol third-axis split (c) and the bear×not-high pool are outside the pre-registered K=6 family and carry no multiplicity correction.
- Bear-regime rows cluster in time (one bear stretch of the sample window); ticker-clustered bounds do not correct for time clustering — a regime-level caveat that applies to every bear cohort here.
- CONFIRMED flips the trend story (bull marginal positive, bear marginal flat): the confirmed filter interacts with regime, consistent with the known confirmed+no_flip edge being a different cut than the bear/high-vol screens.


# Appendix Q2 - full run report

# Q2 rev_early_highvol_coherence — CELL A results

Is the EARLY x high-vol reversal cell coherent with the highvol edge, or a
multiple-comparison artifact? Cell A only; cell B (the rvol-gated-book read) is
deferred (needs sharded detector re-runs).

- Script: `scripts/replay_early_highvol.py`
- Run: `cd <worktree> && PYTHONPATH=src python scripts/replay_early_highvol.py --cache-root "C:/Users/Oliver/source/repos/swing-screener/.cache"`
- Input: pinned D_dump shards `queue_experiments/D_dump_s0..s7_slip0.05.parquet`
  (511-name corpus as-of 20260703; 0.05 ATR slippage already baked into
  `realized_r`). 8 shards, 87,090 rows, 511 tickers; 70,277 reversal rows.
- Stats: house `breakdown` -> `summarize` -> `_clustered_ci_low` (ticker-clustered
  bootstrap 2.5th-percentile lower bound, never more optimistic than IID;
  `ci_high` is the IID upper bound per house convention). Cluster unit = ticker.

## Pre-registered decision rule (cell A, restated verbatim)

PASS iff ALL of:

1. clustered 95% CI lower bound > 0 (realized_r already net of 0.05 ATR slippage)
2. n_closed >= 20
3. clusters >= 8
4. CI half-width <= 0.10R

Bonferroni note: the gated comparison family is exactly ONE cell (early x high);
the four control contrasts are reported, never gated. Bonferroni over a family of
1 leaves the plain 2.5th-percentile clustered bound unchanged.

Half-width definition used for the gate: `expectancy_r - clustered ci_low`
(`hw_low`) — the hardened bound and the most conservative of the three
definitions reported below (all three agree on the pass either way).

## Raw numbers

| cell | role | exp R | clustered ci_low | ci_high (iid) | hw_low | hw_iid | hw_sym | n_closed | clusters | thin | fill% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| early x high | PRIMARY | +0.171 | +0.109 | +0.227 | 0.063 | 0.055 | 0.059 | 1940 | 219 | False | 71% |
| early x med | control | +0.015 | -0.002 | +0.031 | 0.018 | 0.016 | 0.017 | 25064 | 505 | False | 73% |
| early x low | control | -0.003 | -0.038 | +0.026 | 0.036 | 0.029 | 0.032 | 7583 | 373 | False | 75% |
| confirmed x high | control | +0.101 | -0.023 | +0.222 | 0.124 | 0.121 | 0.123 | 407 | 113 | False | 38% |
| confirmed x med | control | +0.040 | +0.009 | +0.071 | 0.031 | 0.031 | 0.031 | 6898 | 503 | False | 42% |

`hw_low` = exp − clustered ci_low (gated); `hw_iid` = 1.96 x stderr;
`hw_sym` = (ci_high − ci_low)/2. `fill%` = filled / all cohort rows (open/pending
included); n_closed counts closed+filled rows with a realized R.

## Verdict per the pre-registered rule

| bar component | required | actual | result |
|---|---|---|---|
| clustered 95% lb > 0 | > 0 | +0.109 | PASS |
| n_closed >= 20 | >= 20 | 1940 | PASS |
| clusters >= 8 | >= 8 | 219 | PASS |
| CI half-width <= 0.10R | <= 0.10 | 0.063 (iid 0.055, sym 0.059) | PASS |

**VERDICT: cell A PASS** — early x high-vol reversal expectancy is +0.171R with a
ticker-clustered 95% lower bound of +0.109R over 1,940 closed trades on 219
ticker clusters, net of 0.05 ATR slippage.

Coherence read (descriptive, not gated): the signal is concentrated exactly where
the highvol thesis says it should be. Early cools sharply off high vol
(high +0.171 -> med +0.015 -> low −0.003), and within high vol the early cell is
the deep, bounded one while confirmed x high is thin (n=407, lb −0.023, hw 0.124)
— consistent with a real vol-conditioned early edge rather than a lucky cell in a
5-way scan.

## Caveats

- **Cell B deferred.** The rvol-gated-book read (does gating the live book on
  rvol reproduce this cell's economics?) requires sharded detector re-runs and
  was explicitly out of scope today. Full CONFIRMED-COHERENT status also needs
  the 0.10-slippage re-runs. Today's verdict is cell A only.
- Controls are reported, not gated; no multiplicity correction was applied to
  them, so their bounds are descriptive. Confirmed x high in particular is thin
  (n=407, 38% fill) and its interval fails the half-width bar it was never held to.
- `ci_high` is the IID upper bound by house convention (only the lower bound is
  hardened); all three half-width definitions pass the 0.10R bar regardless.
- Single pinned corpus (as-of 20260703, `default` variant only in D_dump);
  results inherit any corpus-construction choices baked into those dumps.
- `volatility_tier` string in the dumps is `med` (not `medium`); cells are
  labeled accordingly.


# Appendix Q3 - full run report

# Q3 cont_medvol_strict_gate — does a strict med-vol continuation cut reach lb > 0?

**Verdict: PARK.** No cell in the pre-registered grid comes anywhere near a positive
corrected lower bound. The best cell (`ma_rising & depth>=1.0 & trend_dist>=2.0`)
grades **-0.044R with corrected lb -0.151** (n=396, 214 clusters). This closes both
med-vol open-question bullets in `edge/continuation.md` as a null (the closure itself
lands via the reflection cycle, not this run).

- Script: `scripts/replay_cont_medvol_gates.py`
- Run: `cd <worktree> && PYTHONPATH=src python scripts/replay_cont_medvol_gates.py --cache-root <repo>/.cache`
- Substrate: D_dump shards (slip 0.05, pinned as-of 20260703), `variant=="default"
  & play_type=="continuation" & volatility_tier=="med" & status=="closed"` —
  **9,986 rows, 497 tickers, 9,642 closed-filled** (the pinned-corpus analogue of the
  reflect corpus's -0.14R / lb -0.20 / n=1879 slice; baseline here reproduces it:
  exp **-0.150**, lb2.5 **-0.171**).
- Features recomputed at the trigger bar (bars strictly before `opened_date`,
  `detect_last_bar` re-pass — the `replay_cont_rank_sweep.py` pattern, no lookahead)
  for 9,984 of 9,986 signals; merged book 9,984 rows (100% of substrate; 2 rows lost
  to insufficient history/no context).
  - `depth = (ema_fast - swing_low)/atr`
  - `trend_dist = (trigger_close - ema_slow)/atr`
  - `ma_rising = ema_slow.iloc[-1] > ema_slow.iloc[-6]`

## Pre-registered decision rule (restated verbatim)

Grid fixed in advance, no post-hoc tuning: cells = `ma_rising AND depth >= d AND
trend_dist >= t` for (d, t) in {0.5, 1.0} x {1.0, 2.0} (4 cells) PLUS the two marginal
single-gate cuts (`ma_rising`-only; `depth>=1.0`-only) = 6 comparisons, Bonferroni x6
(one-sided). **ESCALATE iff any cell has corrected clustered 95% CI lower bound > 0**
(net 0.05 ATR slippage, already baked into `realized_r`) **with n_closed >= 20 and
clusters >= 8. Otherwise PARK.**

Corrected bound = house ticker-clustered bootstrap (`_clustered_ci_low`) at the
Bonferroni-corrected one-sided percentile 100*(0.05/6) = 0.833 (IID fallback/floor at
z = 2.394); `lb2.5` is the standard house 2.5th-percentile bound for reference.

## Results (raw numbers)

Baselines:

| cohort | exp R | lb2.5 | lb_corr | n_closed | clusters | win |
|---|---|---|---|---|---|---|
| baseline med-vol (full substrate) | -0.150 | -0.171 | -0.178 | 9642 | 497 | 0.32 |
| baseline med-vol (merged, reference) | -0.150 | -0.171 | -0.178 | 9640 | 497 | 0.32 |

The 6 pre-registered comparisons — PASS = the gated cell (the decision cohort),
FAIL = its complement; delta = pass-vs-fail clustered two-sample lower bound
(descriptive, uncorrected):

| # | cell | cohort | exp R | lb2.5 | lb_corr | n_closed | clusters | win | delta lb2.5 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | ma_rising & depth>=0.5 & trend_dist>=1.0 | PASS | -0.114 | -0.148 | -0.156 | 3359 | 461 | 0.34 | +0.013 |
|   |  | FAIL | -0.169 | -0.194 | -0.201 | 6281 | 491 | 0.31 | |
| 2 | ma_rising & depth>=0.5 & trend_dist>=2.0 | PASS | -0.111 | -0.160 | -0.168 | 1541 | 397 | 0.34 | -0.007 |
|   |  | FAIL | -0.157 | -0.182 | -0.188 | 8099 | 496 | 0.32 | |
| 3 | ma_rising & depth>=1.0 & trend_dist>=1.0 | PASS | -0.058 | -0.126 | -0.141 | 913 | 313 | 0.36 | +0.036 |
|   |  | FAIL | -0.160 | -0.182 | -0.187 | 8727 | 496 | 0.32 | |
| 4 | ma_rising & depth>=1.0 & trend_dist>=2.0 | PASS | -0.044 | -0.136 | -0.151 | 396 | 214 | 0.36 | +0.017 |
|   |  | FAIL | -0.154 | -0.176 | -0.182 | 9244 | 497 | 0.32 | |
| 5 | ma_rising (marginal) | PASS | -0.150 | -0.171 | -0.178 | 9640 | 497 | 0.32 | -inf |
|   |  | FAIL | +0.000 | +0.000 | nan | 0 | 0 | 0.00 | |
| 6 | depth>=1.0 (marginal) | PASS | -0.061 | -0.135 | -0.148 | 958 | 321 | 0.35 | +0.021 |
|   |  | FAIL | -0.160 | -0.182 | -0.187 | 8682 | 496 | 0.32 | |

## Verdict per the rule

Every cell has n_closed >= 20 and clusters >= 8 except the degenerate `ma_rising`
complement (which is not a decision cohort). **Zero of the 6 cells has corrected lb
> 0 — the closest is cell 4 at -0.151. PARK.** Even the *uncorrected* 2.5th-percentile
bounds are all <= -0.126, and no cell's point expectancy is positive, so the null is
not a power artifact of the Bonferroni correction.

Secondary read (descriptive only): the strictness gradient is real but tiny —
expectancy improves monotonically with stricter depth/trend_dist (-0.150 baseline ->
-0.044 in the strictest cell) and three of the pass-vs-fail delta lower bounds are
slightly positive (best +0.036). Strict gating removes the *worst* med-vol
continuation trades but the surviving cohort is still confidently loss-making.

## Caveats

- **`ma_rising` is degenerate on this substrate**: true at 9,640/9,640 merged
  closed-filled rows. The continuation detector's trend precondition (ema_fast >
  ema_slow, close > ema_slow) apparently implies a rising slow MA at every trigger on
  this corpus, so the marginal cut #5 is a no-op (identical to baseline) and the 4
  grid cells effectively reduce to depth x trend_dist cuts. The Bonferroni x6
  correction was kept as pre-registered (conservative).
- The `ma_rising` FAIL row is an empty cohort: exp/lb print as +0.000/+0.000 (house
  `summarize` empty-input behavior), lb_corr is nan, delta is -inf (documented
  `clustered_two_sample_delta_low` empty-book sentinel). None of these are decision
  numbers.
- Replay book, pinned corpus as-of 20260703, net 0.05 ATR slippage baked into
  `realized_r`; momentum_flip/time_stop exits are never haircut (house-wide caveat).
- Features come from a detector-context re-pass at bars strictly before
  `opened_date` — identical to `replay_cont_rank_sweep.py`'s recompute, no lookahead —
  but 2 of 9,986 substrate rows had no recomputable context and drop out of the merge.
- The corrected bound uses a normal-quantile IID floor (z=2.394) inside
  `_clustered_ci_low`'s min(); at the 0.833rd bootstrap percentile with 1,000
  resamples the tail estimate is coarse (~8 order statistics), but every bound is so
  far below zero that resolution is immaterial to the verdict.
- The substrate filter is `status=="closed"` as pre-registered; expectancy/bounds are
  computed on closed-filled rows with non-null `realized_r` (house
  `_is_closed_filled`), hence n=9,642 vs 9,986.


# Appendix Q4 - full run report

# Q4 — rev_rotation_cluster_rs: sector-relative rotation tag (2026-07-25)

Script: `scripts/replay_rotation_tag.py`
Run: `PYTHONPATH=src python scripts/replay_rotation_tag.py --cache-root <repo>/.cache --as-of 20260703`
(project venv python; run from the worktree root so `swing_screener` resolves into the worktree)

## Question

The legacy rotation notion — same-sector, same-flip-day `cluster_size >= 4` — was believed to
tag ~85% of the reversal book, i.e. a non-discriminating label. Can a sector-RELATIVE
redefinition (the cluster must also beat SPY on the flip day) discriminate AND separate
outcomes, or should the rotation thread be retired?

## Pre-registered decision rule (fixed before looking; restated verbatim)

Single binary cut: `rotation_tagged := cluster_size >= 4 AND (cluster mean member flip-day
close-to-close return − SPY same-day close-to-close return) >= +1.0%`.

1. **Discrimination** — tagged share of the reversal book < 50%.
2. **Edge** — `clustered_two_sample_delta_low` (ticker clusters) of `realized_r`
   tagged-vs-untagged on closed filled rows, 95% lower bound > 0 at slip 0.05
   (already net of 0.05 ATR), with `n_closed >= 20` and >= 8 distinct tickers tagged.
3. **Robustness** — direction (mean tagged − mean untagged > 0) holds on
   `.cache/rotation_entry/C_robust_s*_slip0.10.parquet`, `variant=="default"`.

Outcomes: fail (1) → retire the rotation thread for good; pass (1) fail (2) → record
falsified hunch, retire thread; pass (1)+(2) at 0.05 AND direction holds at 0.10 → promote as
a ranking/attribution dimension ONLY. **No gate under any outcome.** Comparison family = the
one pre-registered contrast (K=1; Bonferroni over K=1 is the identity — the 2.5th-percentile
clustered bound is the graded bound). The excess×cluster-size bands are descriptive only.

## Setup facts (from the run)

- Book: D_dump shards slip 0.05, `play_type=="reversal"`, variant `default` — 70,277 rows,
  511 tickers (missed/invalidated/pending rows included in the cluster universe; graded
  cohort = closed filled rows only).
- Sector join: 502 sector files as-of 20260703; 1.0% of book rows have no sector
  (fail-open: they join no cluster, can never be tagged — mirrors `cap_by_sector`).
- Flip re-pass (signal-only detector re-pass, `reversal_confirm_window=3` config):
  70,198 / 70,277 unique (ticker, opened_date) pairs matched (79 unmatched = 0.11%,
  excluded from clusters, never tagged). Re-derived strength agreed with the dump's
  stamped strength on **100.0%** of the 70,198 matched rows.
- Robustness book: C_robust slip 0.10 `variant=="default"` — 62,444 rows, 511 tickers
  (its (ticker, opened_date) pairs are a strict subset of D_dump's, as expected: it was
  booked under the pre-flip `reversal_confirm_window=1` default). Clusters counted within
  its own full signal universe. It has every column the delta needs (ticker, opened_date,
  fill_status, status, realized_r) — no faithful-repeat compromise was necessary.

## Sanity check — legacy tag reproduction

| tag | share of all book rows | share of closed fills |
|---|---|---|
| LEGACY: `cluster_size >= 4` (no return condition) | **81.5%** | 80.9% |

Expected ~85%; 81.5% reproduces the non-discrimination finding (the "~85%" was an
approximate recollection — the qualitative fact stands: 4 out of 5 reversal signals carry
the legacy "rotation" label, so it labels nothing).

## Grade (1) — discrimination

| tag | share of all book rows | share of closed fills | bar |
|---|---|---|---|
| ROTATION: `cluster_size >= 4 AND excess >= +1.0%` | **41.0%** | 38.8% | < 50% → **PASS** |

## Grade (2) — edge at slip 0.05 (D_dump, closed filled rows)

| side | exp R | ci_low | n_closed | clusters |
|---|---|---|---|---|
| tagged | +0.024 | +0.002 | 17,187 | 501 |
| untagged | +0.024 | +0.005 | 27,106 | 511 |

Delta (tagged − untagged): point **+0.000**, ticker-clustered 95% lower bound **−0.029**.
Gates: n_tagged 17,187 ≥ 20 ✓, tagged clusters 501 ≥ 8 ✓. Lower bound > 0 → **FAIL**.
The two sides are *identical* to three decimals — the sector-relative tag carries zero
outcome information on this book.

## Grade (3) — robustness at slip 0.10 (C_robust, variant=default)

| side | exp R | ci_low | n_closed | clusters |
|---|---|---|---|---|
| tagged | −0.016 | −0.039 | 15,920 | 501 |
| untagged | −0.013 | −0.032 | 25,182 | 511 |

Delta point **−0.003** (> 0 required) → **FAIL** (clustered 95% lower bound −0.035).

Supplementary, NOT graded — same signal set as D_dump at 0.10 cost
(C_robust `variant=="confirm3"`): tagged −0.006 (lb −0.028, n 17,187) vs untagged −0.009
(lb −0.027, n 27,106); delta point +0.003, lb −0.029. Direction flips sign between the two
0.10 books — i.e. the "direction" is noise around zero either way.

## Descriptive bands — excess-vs-SPY × cluster_size (rows | closed fills | mean R)

| size | <0 | 0–1% | 1–2% | >=2% | no-excess |
|---|---|---|---|---|---|
| 1 | 593 \| 425 \| +0.059 | 903 \| 629 \| +0.033 | 793 \| 533 \| −0.015 | 980 \| 590 \| +0.043 | 766 \| 464 \| −0.065 |
| 2–3 | 1,248 \| 859 \| +0.003 | 2,799 \| 1,886 \| −0.024 | 2,534 \| 1,630 \| +0.042 | 2,415 \| 1,423 \| −0.073 | 0 |
| 4–6 | 1,336 \| 922 \| −0.017 | 4,227 \| 2,773 \| +0.048 | 4,096 \| 2,598 \| +0.020 | 2,901 \| 1,754 \| −0.056 | 0 |
| 7–9 | 936 \| 698 \| −0.057 | 3,610 \| 2,399 \| −0.071 | 3,321 \| 2,037 \| −0.024 | 2,019 \| 1,150 \| +0.035 | 0 |
| 10+ | 3,760 \| 2,595 \| +0.070 | 14,592 \| 9,280 \| +0.065 | 10,650 \| 6,538 \| −0.009 | 5,798 \| 3,110 \| +0.171 | 0 |

No monotone structure in either dimension. The eye-catching 10+/≥2% cell (+0.171 mean R,
3,110 closed fills) is post-hoc, unregistered, and contradicted by its neighbors (7–9/≥2%
+0.035; 10+/1–2% −0.009) — classic multiple-comparisons bait, reported for completeness only.

## VERDICT (per the pre-registered rule)

- (1) discrimination: 41.0% < 50% → **PASS**
- (2) edge @0.05: lower bound −0.029, not > 0 → **FAIL**
- (3) direction @0.10 (default book): −0.003, not > 0 → **FAIL** (moot given (2))

**OUTCOME: pass (1), fail (2) → record falsified hunch; RETIRE the rotation thread.**

The sector-relative redefinition *does* fix the discrimination problem (41% vs 81.5%
tagged), so the tag is now a real label — it just labels nothing that matters: tagged and
untagged reversal trades earn identical expectancy at 0.05 cost (+0.024 vs +0.024) and the
clustered separation bound is decisively below zero. Under the pre-registration this is the
end of the rotation thread: no gate, no ranking dimension, no further redefinitions.

## Caveats

- The flip day is re-derived, not stamped: 79/70,277 signals (0.11%) could not be
  re-derived and were conservatively left untagged; 1.0% of rows have no sector file and
  likewise can never be tagged. Both are fail-open exclusions on the tag side and are far
  too small to move a delta that sits at +0.000.
- The 100% strength agreement on matched rows certifies the re-pass reproduces the exact
  booked detector contexts (current worktree `StrategyConfig()` == the D_dump book config).
- The legacy share reproduced at 81.5% vs the quoted ~85% — same qualitative fact; the
  quoted figure was approximate.
- The 0.10 robustness book was booked under the older `reversal_confirm_window=1` default
  (62,444 rows vs 70,277); the pre-registration named that book explicitly, so it was
  graded as specified. The same-signal-set confirm3@0.10 supplementary repeat (point
  +0.003, lb −0.029) does not change the conclusion — with (2) failed, (3) is moot.
- Cluster membership is deduped per (sector, flip_date, ticker); a cluster's excess uses
  the mean of member tickers' flip-day close-to-close returns minus SPY's same-day
  close-to-close, all off the pinned 20260703 snapshots. Clusters whose excess is
  uncomputable (NaN) are untagged.
- Single pre-registered contrast (K=1), so no Bonferroni deflation was applied; the graded
  bound is the house 2.5th-percentile ticker-clustered bootstrap, net of slippage already
  baked into `realized_r`.


# Appendix Q5 - full run report

# Q5 — rev_tier_ladder_certification: has the conviction_tier ladder ever been certified?

**Script:** `scripts/replay_tier_ladder.py` (run from the worktree root, repo venv):

```
PYTHONPATH=src python scripts/replay_tier_ladder.py --cache-root "C:/Users/Oliver/source/repos/swing-screener/.cache"
```

**Data:** pinned 511-name replay corpus, as-of 20260703. Primary book = the 8
`queue_experiments/D_dump_s*_slip0.05.parquet` shards, `play_type=="reversal"` &
`variant=="default"` (the confirm-window-3 book; 0.05 ATR slippage already baked into
`realized_r`): 70,277 rows / 511 tickers / 44,293 closed fills. Test (B) features
(`confirm_lag`, `volume_ratio`, `strength`) recomputed per booked trade via the
`replay_rank_sweep.py` detector-context pattern (`build_frame` per ticker under the current
`StrategyConfig()` — the same config that produced the book — `detect_reversal` on the frame
strictly before each `opened_date`, merged on `(ticker, opened_date)`).
**Coverage: 70,198 / 70,277 book rows matched (99.9%); stamped-vs-recomputed strength
agreement 100.0%** — the recompute reproduces the booked contexts.

## Pre-registered decision rule (restated verbatim)

K = 4 one-sided contrasts, Bonferroni alpha/4 (alpha = 0.05 one-sided, so the deciding
bound is the ticker-clustered bootstrap **1.25th percentile** of the two-sample delta):

- **A1** stamped premium vs rest; **A2** stamped premium+strong vs base
  (stamped tier as booked: premium = high_vol (rvol >= 1.3) AND is_spring);
- **B1** challenger premium' vs rest; **B2** challenger premium'+mid vs base'.

Challenger ladder (stated before computing, then held fixed):
`premium' := strength=="confirmed" AND confirm_lag>=2 AND volume_ratio>=1.3`;
**mid := non-premium' AND (strength=="confirmed" OR volume_ratio>=1.3)** (highvol = the same
1.3 threshold premium' and the stamped `reversal_premium_min_rvol` use); `base'` = the rest
(early flips with rvol < 1.3).

**A ladder is CERTIFIED for conviction-sizing weights iff its top-tier-vs-rest corrected
clustered one-sided lower bound > 0 (net 0.05 ATR, already in realized_r) with
n_closed >= 20 and clusters >= 8 in the top tier.** If neither certifies,
conviction-weighted sizing (Q8) stays blocked and the tier demotes to a surfacing label.
The house 2.5th percentile and a stricter 2.5/4 = 0.625th percentile are reported alongside;
the 1.25 column decides.

## (A) Stamped ladder — D_dump 0.05 book

| tier | n_total | n_closed | clusters | exp R | ci_low(2.5) | fill% |
|---|---|---|---|---|---|---|
| premium | 1412 | 989 | 423 | +0.073 | -0.004 | 70% |
| strong | 32638 | 16814 | 511 | +0.029 | +0.006 | 52% |
| base | 36227 | 26490 | 511 | +0.020 | +0.004 | 73% |

| contrast | hi exp (n, cl) | lo exp (n) | delta | dLo@2.5 | **dLo@1.25** | dLo@0.625 |
|---|---|---|---|---|---|---|
| A1 premium vs rest | +0.073 (989, 423) | +0.023 (43304) | +0.050 | -0.0265 | **-0.0430** | -0.0525 |
| A2 premium+strong vs base | +0.031 (17803, 511) | +0.020 (26490) | +0.012 | -0.0150 | **-0.0195** | -0.0230 |

## (B) Lag-aware challenger ladder — recomputed context, same book

| tier | n_total | n_closed | clusters | exp R | ci_low(2.5) | fill% |
|---|---|---|---|---|---|---|
| premium' | 1246 | 448 | 295 | +0.066 | -0.039 | 36% |
| mid | 29203 | 14648 | 511 | +0.039 | +0.017 | 50% |
| base' | 39749 | 29137 | 511 | +0.016 | -0.000 | 73% |

| contrast | hi exp (n, cl) | lo exp (n) | delta | dLo@2.5 | **dLo@1.25** | dLo@0.625 |
|---|---|---|---|---|---|---|
| B1 premium' vs rest | +0.066 (448, 295) | +0.024 (43785) | +0.042 | -0.0541 | **-0.0636** | -0.0699 |
| B2 premium'+mid vs base' | +0.040 (15096, 511) | +0.016 (29137) | +0.023 | -0.0042 | **-0.0075** | -0.0108 |

## Descriptive 0.10-slippage sidebar (C_robust dumps; NOT in the K=4 family)

Variant `confirm3` (n=70,277 — the same signal set/config as the D_dump default book):

| tier | n_closed | clusters | exp R | ci_low(2.5) |
|---|---|---|---|---|
| premium | 989 | 423 | +0.049 | -0.027 |
| strong | 16814 | 511 | -0.002 | -0.023 |
| base | 26490 | 511 | -0.013 | -0.029 |

| contrast | delta | dLo@2.5 | dLo@1.25 | dLo@0.625 |
|---|---|---|---|---|
| premium vs rest | +0.058 | -0.0263 | -0.0311 | -0.0436 |
| prem+strong vs base | +0.013 | -0.0116 | -0.0153 | -0.0173 |
| premium' vs rest (challenger) | +0.047 | -0.0544 | -0.0744 | -0.0807 |
| prem'+mid vs base' (challenger) | +0.025 | +0.0003 | -0.0031 | -0.0075 |

Variant `default` (legacy window-1 book, n=62,444): premium vs rest delta +0.059,
dLo@2.5 = -0.0247 / dLo@1.25 = -0.0356; prem+strong vs base delta -0.004,
dLo@2.5 = -0.0308. Same picture.

## Verdict (per the pre-registered rule)

- **A1** dLo@1.25 = **-0.0430** (top tier n_closed=989 >= 20, clusters=423 >= 8) — bound <= 0 → **(A) stamped ladder NOT certified**.
- **B1** dLo@1.25 = **-0.0636** (n_closed=448, clusters=295) — bound <= 0 → **(B) challenger ladder NOT certified**.
- Context: A2 dLo@1.25 = -0.0195; B2 dLo@1.25 = -0.0075. Every one of the four
  pre-registered bounds is negative at ALL three percentiles (2.5 / 1.25 / 0.625), so the
  verdict does not hinge on the correction interpretation.

**NEITHER ladder certifies. Per the pre-registered consequence: conviction-weighted sizing
(Q8) stays BLOCKED, and conviction_tier demotes to a surfacing label — it has never been,
and on this corpus is not, a certified outcome separation.** The premium tier's raw edge
(+0.073R, n=989) is real-looking but concentrated/noisy: its own ticker-clustered
expectancy bound is already negative (-0.004 at the house 2.5th percentile), and the
premium-vs-rest separation dies under clustering long before any Bonferroni correction.

## Caveats

- **Forward-book read: N/A offline** (the original spec's descriptive read of the live DB
  is out of scope here; this is the pinned replay corpus only).
- The 0.05-slippage haircut is baked into `realized_r` in the primary book; the 0.10
  sidebar is descriptive robustness only and agrees (no contrast certifies there either;
  the single positive number, +0.0003 at the uncorrected 2.5th percentile for the 0.10
  challenger B2 analogue, is outside the family and vanishes at the deciding percentile).
- Feature recompute matched 99.9% of book rows with 100.0% stamped-vs-recomputed strength
  agreement; the 79 unmatched rows are frames with < 60 bars before the open date or
  no-context edge cases and cannot move any bound.
- Challenger `premium'` fills only 36% of its signals (a 2+-bar-lag confirmed bounce has
  usually run away from the pullback-limit band). Any future sizing story for a lag-gated
  tier must price this in: the tier is small (448 closed fills) *and* hard to fill.
- The B2 contrast (prem'+mid vs base', i.e. "confirmed-or-highvol vs the rest") is the
  closest miss (dLo@1.25 = -0.0075) and the challenger `mid` tier carries a positive own
  bound (ci_low +0.017, n=14,648) — consistent with the existing certified
  reversal-CONFIRMED edge — but per the pre-registered rule that does not certify a
  ladder, and it is a cohort statement, not a top-tier separation.
- One-sided Bonferroni at alpha 0.05/4 = percentile 1.25 was the pre-registered deciding
  bound; the stricter house-style 2.5/4 = 0.625 and the uncorrected house 2.5 columns are
  reported and all agree in sign, so no rounding or threshold choice changes the verdict.
