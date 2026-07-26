# 2026-07-25 strategy review — agent-execution verdict + pre-registered experiment queue

From a 16-agent review workflow (board audit, execution-readiness audit, harness-mechanics
audit, candidate generation, then per-candidate adversarial verification against the actual
code paths). Every queue item below was independently verified for path-reachability,
settled-retest overlap, and harness feasibility before being listed — 1 of 12 candidates was
rejected as a settled retest and replaced by its salvageable residual (Q5).

## Part 1 — Verdict: is anything ready for an agent to execute?

**No strategy qualifies for autonomous real-money execution, and that is the system working
as designed.** The three locks in `settings.can_arm_real_money` require `forward_confirmed`
verdicts plus conviction calibration (`pipeline/autonomy.py:80-124`), and the board has
**zero forward-confirmed (gold) edges** on either book:

- **Continuation** — falsified as a book (-0.161R, n=16,219 pinned; every slice negative;
  re-ranking falsified; the forward score buckets are filling *negative*, live-confirming the
  replay). The last untested selection question (Q3) and entry-economics lever (Q6) are
  queued below to complete the parking case.
- **Reversal** — three replay-screened candidates (bear regime +0.245R lb +0.149; high-vol
  tier +0.246R lb +0.097; low score band lb pinned at +0.0001) but forward n on the standouts
  is **0 (bear) and 1 (high-vol)**. The bear screen is *regime-gated*: the tape is bull, so
  its forward bucket accrues zero — it cannot progress toward gold until a bear tape occurs.
  rev_highvol, the strongest replay finding, is the *slowest-settling* item on the board
  (~4-8+ weeks) because the reversal baseline forward book closes only ~1 trade/day.

**What IS executing today:** prod runs the internal **PaperAdapter** (armed 2026-07-17;
$1,000 simulated equity, 1R=$100, caps 1000 notional / 2R daily loss / 3 concurrent) —
top-5 deep picks per play type dispatch as intents every digest run. The next execution step
on the runbook is the **Alpaca paper-sandbox drill** (Q12): code-complete end-to-end
(LiveAdapter → AlpacaBroker → reconcile_live, brackets, disarm), blocked only on infra
secret plumbing — and it doubles as the experiment that *measures* the 0.05 ATR cost
assumption every promotion decision is conditioned on.

**Nearest decisions on the current board:** the four exit arms (no_flip, partial33_cond,
partial33_chand, be_1r) — they accrue on every signal from both books (~4-5 paired
closes/day) and plausibly settle within weeks. The reversal variants are the laggards.

## Part 2 — Board hygiene (do these first)

1. **Merge PR #151** (`reflection/edge-update`, open since 2026-07-19) — the on-disk board
   is 6 days behind its own reflection (forward_closed 803→1,055 rev / 851→956 cont).
2. **No-op proposal withdrawn** (in this PR): `reversal_volatility_tier_med_1_q` carried
   delta `{max_extension_atr: 2.5}` with `play_type=reversal` — a continuation-only gate
   (`pipeline/analyze.py:133`), the same defect that withdrew 3 of 4 prior proposals,
   re-minted by the 2026-07-16 reflection drafter. It would have swept as a false null in
   the Sunday optimizer run. Root-cause fix is Q10.
3. The queued continuation proposal (`min_pullback_bars: 3`) is path-valid — leave it.

## Part 3 — The queue

Registration note: none of these are `edge/experiments.json` rows or `proposed.json` deltas —
the registry locksteps to live rosters (variants at the 7-book ceiling), and the proposed
channel only expresses `StrategyConfig` deltas and is machine-owned (Sunday reflections
replace all `queued` rows). This dated doc is the pre-registration; results enter the edge
files through the reflection cycle citing it. House bar throughout: ticker-clustered 95% CI,
net of 0.05 ATR slippage (0.10 for robustness), n_closed ≥ 20, clusters ≥ 8; Bonferroni over
each item's stated comparison family.

### A. Free diagnostics on existing pinned dumps (runnable today, zero replay compute)

> **RUN 2026-07-25 — all five complete, every verdict independently re-derived and
> confirmed. Full tables: [2026-07-25-free-diagnostics-results.md](2026-07-25-free-diagnostics-results.md).**
> Q1: NEITHER pattern — bear is the load-bearing axis, high-vol amplifies inside bear
> (bear×high lb +0.218; bull×high fails) → the promotion cohort is bear×high, and Q9's
> highvol-only premium redefinition must be reconsidered (bear-conditioned instead).
> Q2: EARLY×high-vol cell A PASS (+0.171R, lb +0.109, hw 0.063); cell B + 0.10 repeats
> still owed. Q3: PARK — 0/6 cells clear; continuation selection-side closed. Q4: RETIRE
> the rotation thread (discriminates, zero edge). Q5: NOT CERTIFIED (both ladders) — Q8
> sizing stays blocked; conviction_tier is a surfacing label only.

**Q1. rev_bear_highvol_crosstab** — *Are the two standout reversal screens the same trades?*
New `scripts/replay_rev_crosstab.py` (~60 lines, `_Row`+`breakdown` pattern from
`replay_rev_combo.py:66-86`) over `.cache/queue_experiments/D_dump_s*_slip0.05.parquet`
(baseline reversal book, 70,277 rows, min cell n=718 / 112 clusters — cannot come back
underpowered). Output: market_trend × volatility_tier 6-cell table + market_vol third axis +
confirmed-cohort repeat, each cell exp/lb/n/clusters/fill%; print own marginals (pinned
511-name corpus ≠ reflect's unpinned ~100). K=6 Bonferroni. Verdict logic: "one edge counted
twice" if bear∩high clears lb>0 while both off-diagonals fail; "additive" if bear∩¬high and
bull∩high independently clear. **Decision enabled:** picks THE cohort label for the premium
tier's promotion design; resolves the low/high-vol sign-flip question. Forward gold bar
unchanged.

**Q2. rev_early_highvol_coherence** — *Is EARLY×high-vol a coherent edge or a sweep
artifact?* Cell A (free, today): D_dump shards filtered `strength=="early" &
volatility_tier=="high"` (n=1,940, 219 clusters), clustered grade; controls early×{med,low},
confirmed×high reported ungated. Cell B (one re-run): `replay_rev_combo.py --as-of 20260703
--slippage 0.05` ×8 shards, cut `variant=="highvol" & strength=="early"` (gated-book read).
PASS = both cells lb>0 @0.05 with half-width ≤0.10R; CONFIRMED-COHERENT = holds @0.10 (re-run
both at 0.10 — no 0.10 D_dump exists yet). On PASS: record replay-screened, then draft the
tier-extension follow-up + forward-track the facet on the live default book (strength +
volatility_tier already stamped); no surfacing change before forward confirmation. On FAIL:
close edge/reversal.md's EARLY open question as resolved-artifact. Note: EARLY = 67.7% of
pinned detections (82.7% of high-tier closed), not the ~92% previously quoted.

**Q3. cont_medvol_strict_gate** — *The last selection-side continuation question.* New
`scripts/replay_cont_medvol_gates.py` (cloned from `replay_cont_rank_sweep.py`) over D_dump
`variant=="default" & play_type=="continuation" & volatility_tier=="med"` (the -0.14R
lb -0.20 n=1879 slice). Features recomputed at trigger bar: depth=(ema_fast-swing_low)/atr;
trend_dist=(close-ema_slow)/atr; ma_rising=ema_slow rising over 5 bars. Pre-registered grid,
no post-hoc tuning: ma_rising AND depth≥d AND trend_dist≥t for (d,t) ∈ {0.5,1.0}×{1.0,2.0}
+ 2 marginals (K=6 Bonferroni). ESCALATE iff any cell corrected-lb>0 @0.05 (then 0.10
re-walk, then detection-only knobs `min_pullback_depth_atr` / rising-MA gate + forward
variant). PARK otherwise: close both open-question bullets in edge/continuation.md — no
further continuation selection-side experiments without new entry-economics evidence. Note:
these knobs do NOT exist in StrategyConfig — a proposed.json delta cannot express this; the
prior "min_pullback_bars deepens the pullback" framing was a different (path-valid but
weaker) question.

**Q4. rev_rotation_cluster_rs** — *Replace the non-discriminating ~85% sector-cluster tag.*
New `scripts/replay_rotation_tag.py`. Book = D_dump reversal rows incl. `missed` (full-universe
same-day sector counts reproduce the legacy tag as a sanity check); sector = ticker join to
`.cache/sector/{TICKER}_20260703.json` (502 files, fail-open like `cap_by_sector`); flip day
re-derived by a signal-only detector re-pass (booking lags flip by 1-3 bars under
confirm_window=3 — not stamped in dumps). Pre-registered binary tag: cluster_size ≥ 4 AND
(cluster mean flip-day return − SPY same-day) ≥ +1.0%; descriptive bands reported, only the
binary cut graded. Grade: (1) tagged share < 50%; (2) `clustered_two_sample_delta_low`
tagged-vs-untagged lb > 0 @0.05; (3) direction holds on the 0.10 robustness book
(`.cache/rotation_entry/C_robust_s*_slip0.10.parquet`, variant=="default"). Fail (1) → retire
the rotation thread for good. Pass all → ranking/attribution dimension ONLY (pre-committed:
no gate under any outcome).

**Q5. rev_tier_ladder_certification** — *(replaces the rejected tradelog-feature-mining
candidate — that was a settled retest of the 2026-07-03 rank sweep).* The one uncertified
residual: the `conviction_tier` ladder itself (premium/strong/base, `reversal.py:246-259`)
has never been certified as a separation on any book, omits confirm_lag (the sole certified
orderer), and its spring ingredient failed re-validation (2026-07-12, lb -0.021) — yet it is
exactly what conviction sizing (Q8) consumes. New `scripts/replay_tier_ladder.py` on the
pinned corpus, K=4 Bonferroni: (A) stamped ladder — premium-vs-rest and premium+strong-vs-base
clustered deltas; (B) lag-aware challenger premium' = (confirmed AND confirm_lag≥2 AND
volume_ratio≥1.3) — same contrasts. A ladder is CERTIFIED for sizing weights iff its
top-tier-vs-rest corrected lb > 0 @0.05 (report 0.10). Winner's tiers replace the assumed
2.0/1.0/0.5 in `_DEFAULT_CONVICTION_WEIGHTS`. Neither certifies → Q8 stays blocked and the
tier demotes to a surfacing label. Forward book reported descriptively only (post-epoch
filters leave it too thin to certify).

### B. Offline replay sweeps (pinned corpus as-of 20260703, sharded)

**Q6. cont_ceiling_sweep_then_park** — *The last continuation entry-economics lever, then
park on a null.* (Retest-limit entry is settled-dead per 2026-07-04 MAE studies — worse at
every depth; `ceiling_atr_mult` is what remains: it moves fill price, R denominator,
min_target_r floor, and degenerate-zone rejection — `entry_zone.py:58-81`.) Two walks added
to `replay_queue_experiments.py` (≤3 variants/walk): C_ceil_lo {0.15, 0.25, default-0.35
anchor (must reproduce -0.161R/n≈16.2k)}, C_ceil_hi {0.45, 0.55, 0.65}; sharded, both cost
levels; per-cohort clustered bounds ("same pinned corpus", NOT paired — trade sets differ
per ceiling); reversal cohorts must be identical across values (free contamination check).
NULL (no value's continuation cohort lb > 0 @0.05) → execute the pre-authorized parking
rule: human-gated PR adding surfacing-only `surface_continuation: bool` (mirroring
`reversal_surface_confirmed_only`; continuation stays detected/scored/shadow-booked; the
registered cont_volband/extguard_tight books keep accruing as formal arbiters), flipped
False. POSITIVE → replay-screened ceiling; promotion needs a retired variant slot first.
One-shot; no grid extension without a new pre-registration.

**Q7. rev_stop_width_sweep** — *Convert near-miss winner stopouts (11.3% of reversal winners
see MAE ≥ 0.8R, 3× the continuation rate).* Stage 1 (no src change): throwaway
`scripts/replay_rev_stopwidth.py`, variants stop_buffer_atr {default 0.25, 0.35, 0.50,
0.75}, reversal-only filter, per-tier cuts, both cost levels. NOT an arm (initial stop is
zone geometry — arm-independent by construction, `shadow.py:104-106,161`) and NOT
`play_type: exit` (invalid enum — would settle against an empty book). Promote a width to
stage 2 ONLY if clustered lb vs default > 0 @0.05 AND upper > 0 @0.10. Stage 2 (conditional):
add `reversal_stop_buffer_atr: float | None = None` consumed at `reversal.py:214` (so the
shared knob doesn't drag continuation), forward variant `rev_stop_wide` under the standard
registry row — requires retiring a settled variant first (7-book ceiling; rev_confirm1 /
rev_retrace786 are nearest-to-settled).

### C. Harness / infra builds (no market hypothesis; throughput and validity multipliers)

**Q8. sizing_harness_per_trade_risk** — *The cross-cutting thesis's actual exploitation
mechanism: size the good cohort, don't stack gates.* Sequenced: (1) prereq = Q9's tier
redefinition + `TIER_V2_STAMPED_FROM` epoch constant (never blend tier definitions in
conditioned stats); (2) analytics extension `size_weighted_delta(trades, weights)` →
(weighted−plain) per clustered bootstrap resample (reuse `_clustered_ci_low` seam;
`size_weighted_expectancy` is point-estimate-only today); (3) extend `replay_sizing.py` to
the pinned corpus, both cost levels, ladders premium ∈ {1.5, 2.0, 3.0} × base ∈ {0.5, 1.0},
strong=1.0, over the REDEFINED tiers (or Q5's certified ladder). Adoptable ladder =
(weighted−plain) clustered lb > 0 @0.05, non-negative @0.10. LIVE adoption stays gated on
rev_highvol settling positive (doc_ref addendum on that row — no new slot). Rejected forms
(verified): forward sizing arm (paired delta ≡ 0 — R is size-invariant), registry kind
"sizing" (schema rejects), proposed.json entry (play_type unloadable).

**Q9. rev_premium_tier_redefinition** — *The promotion package for tiered surfacing.* Not
registerable anywhere (hardcoded `high_vol and is_spring`, `reversal.py:254-256`; the spring
stack was a 250-name artifact — full-universe highvol-only: lb +0.038 @0.05 / +0.014 @0.10,
n=6,732). (A) Code change: premium = high_vol only (spring stays computed; "strong" keeps
spring/confirmed); update stale comments (`config.py:221-231`, `notify/select.py:97-98`);
add TIER_V2 epoch beside `TIER_STAMPED_FROM`. Do NOT flip `reversal_surface_premium_only`.
(B) Diagnostic: the documented expected-picks/week artifact `config.py:230` requires —
forward: weekly `variant="rev_highvol" & would_surface=True` counts since 2026-07-12;
backcheck: full-universe confirmed ∩ rvol≥1.3 ∩ top-5 from the pinned rev_combo dumps.
(Volume_ratio is not persisted on Signal/PaperTrade — the rev_highvol book is the honest
forward source.) (C) The surfacing flip remains a separate human-gated PR conditional on
rev_highvol settling positive + the picks/week doc. Produces no verdict itself; burns no slot.

**Q10. proposal_reachability_guard** — *Stop minting no-op proposals (4 of the last 5
reversal proposals were path-unreachable).* (A) The immediate withdrawal shipped in this PR.
(B) Root-cause: make `_DRAFT_TOOL` / `_DRAFT_SYSTEM` play-type-conditional with code-owned
example knob lists + "a wrong-play-type knob will be rejected" sentence. (C) Smoke guard in
`reflect.draft_variants`: replay each surviving candidate on a ~30-50-ticker subsample of
the in-scope `replay_frames`; canonicalize per-trade tuples scoped to the proposal's
play_type; identical multisets with ≥30 scoped default trades → DROP as unreachable;
identical-but-thin → queue with "smoke-inconclusive" warning; replay raise → fail-safe queue
nothing. Docstring caveat: certifies sweepable, not live-promotable (`max_vix_rank` is
replay-only). (D) Acceptance tests, not a CI bar: all 4 historical no-op deltas REJECTED
(incl. the pooled-book control that kills the unscoped-diff bug); reachable deltas ACCEPTED
({reversal_retrace_frac: 0.618} — economics-only, kills the fill-ID-only bug;
{reversal_min_flip_rvol: 1.3}; continuation {min_pullback_bars: 3}); tiny-corpus path queues
with warning. Success metric: zero path-unreachable proposals reach `queued` from here on.

**Q11. rev_forward_throughput_boost** — *Attack the real bottleneck: forward n (~1 reversal
close/day), not idea supply.* Separate shadow-extras universe (~300-500 curated liquid
names, `universe_shadow_extra.csv`, disjoint from the 503-name seed) booked shadow-only:
new `shadow_universe_path` param; extras get 1h+1d fetch + latest_bars (advance/resolve need
bars) but SKIP market_cap/sector; screened under base AND every alt variant config; booked
via separate `_shadow_candidates(prior_extra, None)` calls (`would_surface=None` — gold facet
excluded by construction; NEVER merge extras into `prior` or the top-5 re-rank corrupts the
existing gold book). Digest/Signal/universe table stay byte-identical (guard test required).
Staged rollout (+150 first; abort tranche if n_failed/universe > 10%, runbook hard stop 25%;
extras with open trades must keep being fetched until flat). Record cutover date in
rev_highvol provenance; report pooled AND core-only deltas. Success: trailing-28d reversal
research-book close rate ≥ 1.5× pre-cutover within 6 weeks; < 1.2× → halt, fall back to the
0.125R escape-hatch decision at the ~2026-09 checkpoint. What accelerates is the
shadow-book/variant settle (rev_highvol's registered arbiter) — NOT the gold facet, which is
rank-capped and universe-invariant.

### D. Execution path (the drill that is also an experiment)

**Q12. premium_tier_paper_execution_drill** — *Measure the assumption every promotion keys
on.* The 0.05 ATR cost level was adopted (PR #75), never measured. Alpaca paper sandbox
bypasses all arming locks by design (`execution.py:469`; paper host → `is_real_money()` =
False). Env: `SWING_EXECUTION_MODE=live`, `SWING_BROKER=alpaca`, paper keys, host unset,
**`SWING_RISK_PER_TRADE_DOLLARS=100` (REQUIRED — without it every intent sizes to 0 shares
and Alpaca 422-rejects)**, `SWING_MAX_CONCURRENT=10`. Code: ~15-line guard skipping
`intent.shares <= 0` dispatch; new `scripts/grade_paper_drill.py` joining `account="live"`
vs `account="research"` twins on (ticker, timeframe, play_type, run_date): day-1 fill parity
ONLY in v1 (sim rests limits 5 bars; live is TIF=day — ungraded this bakes in a false
"sim-optimistic" read), entry slippage in ATR units, exit slippage on stop/target-matched
exits only; premium-tier subset broken out in GRADING, not routing (no premium routing
exists; restriction only throttles n). Ops rule: manually flatten live twins of
time-stopped sim positions. Infra: Alpaca paper keys into Key Vault + jobs.bicep secretDefs
(none exist today — the only blocker). Stopping rule: n ≥ 20 live fills across ≥ 8 clusters
AND slippage CI half-width ≤ 0.025 ATR; CI upper ≤ 0.05 ATR → certify the 0.05 bar; lb >
0.05 → promotions must clear the measured level. Futility: < 10 fills after 8 weeks → halt
and diagnose. Caveats inherited: reconcile runs at evening cadence only (book dark
intraday; brackets protect); in-run kill switch relies on disarm CLI/cockpit, not env flips.

## Part 4 — Sequencing

Week 1 (all free, existing dumps): Q1, Q2 cell A, Q3, Q4, Q5 — five decisions for zero
replay compute. Then: Q6 + Q7 sweeps (sharded, ~a day of walks), Q10 guard (before the next
Sunday reflection drafts more proposals), Q9 tier redefinition PR. Then: Q8 sizing sweep
(needs Q9 + ideally Q5), Q11 staged rollout, Q12 infra + drill. Q12 and Q11 are the two
items that shorten the calendar path to an agent-executable strategy: Q11 multiplies forward
evidence on the one live promotion candidate; Q12 certifies the cost bar and proves the
order flow the eventual agent will use.
