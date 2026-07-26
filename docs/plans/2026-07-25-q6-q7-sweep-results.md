# 2026-07-25 Q6 + Q7 sweep results (and the Part-A target-vintage re-grade)

Runs of the two pre-registered offline sweeps from
[2026-07-25-strategy-review-experiment-queue.md](2026-07-25-strategy-review-experiment-queue.md)
on the pinned 511-name corpus (as-of 20260703, 0.05 ATR slippage baked into realized_r,
ticker-clustered 95% CIs, seeded house bootstrap). Walk vehicle: four new sharded walks in
`scripts/replay_queue_experiments.py` (C_ceil_lo/C_ceil_hi for Q6; R_stop_a/R_stop_b for
Q7 — a deviation from Q7's original standalone-script plan, for sharding), 32 shard runs,
zero failures. Every verdict was adversarially verified: an independent agent re-derived the
load-bearing numbers from the raw parquets and reproduced them exactly (Q6's cross-walk
identity was re-verified bitwise; Q7's bounds were additionally reproduced under a second
bootstrap seed). Analysis scripts: `replay_ceil_sweep.py`, `replay_rev_stopwidth.py`,
`replay_parta_regrade.py`. All require the repo venv python.

## Verdicts

| sweep | verdict |
|---|---|
| **Q6 ceiling_atr_mult 0.15–0.65** | **NULL — the parking rule activates.** No value gets the continuation cohort's clustered lb above 0 (best: 0.15 at −0.103R, lb −0.120, on a healthy 93.0% fill, n=15,467, 511 clusters — no fill-collapse artifact). The gradient is perfectly monotone (paired vs default: +0.079R at 0.15 → −0.058R at 0.65): entry price is a real lever, but no price rescues the edge. Sanity anchor reproduced the pinned book exactly (−0.1613R, n=16,221, bitwise-identical to D_dump continuation); all six reversal books byte-identical (contamination refuted). The 0.10 robustness re-walk is moot under NULL. Entry-economics decomposition is now COMPLETE: selection, timing, ordering, gates, and entry price are all falsified — `surface_continuation` parking PR is pre-authorized. |
| **Q7 stop_buffer_atr 0.35/0.50/0.75** | **FALSIFIED — no width promoted; 0.25 stands.** All three widths grade negative vs default (delta points −0.0033/−0.0044/−0.0055; clustered lbs ≈ −0.028; n≈44.2k, 511 clusters). Mechanism: near-miss stopouts convert to time_stops, not targets; the drag is worst in bear (deltas −0.008/−0.015/−0.029), i.e. wider stops hurt most exactly where the edge lives. Note realized_r is per-width own-R (wider = 1.08–1.41× bigger dollar denominator), so the per-R comparison understates the per-dollar cost. The 0.10 re-walk runs only on a 0.05 positive — not needed. |

## Side finding — D_dump reversal book is a stale target vintage (and the Part-A re-grade)

The pre-existing `D_dump` shards (2026-07-03) predate the `reversal_retrace_frac`
0.786 → 1.0 default flip: identical signals/entries/stops/fills, but legacy targets —
`realized_r` differs on 10,739 of 70,277 reversal rows (84.7% agreement; mean-R shift
+0.019 kinder under the current rule). The Part-A reversal diagnostics (Q1/Q2/Q4/Q5) were
graded on that legacy book. All four were re-graded on the fresh current-default book
(R_stop_a `default`, deterministic-identical to C_ceil_lo `default`):

| diagnostic | legacy (recorded) | current-target book | verdict |
|---|---|---|---|
| Q1 bear×high | +0.299R, lb +0.218, b6 +0.187 | **+0.339R, lb +0.255, b6 +0.233** (n=1,225) | SURVIVES, strengthens |
| Q1 bull×high | lb −0.055 | lb −0.022 (still fails) | SURVIVES |
| Q1 CONFIRMED bear×high | raw lb +0.057, b6 −0.003 | raw lb +0.087, **b6 +0.019** (tail-thin; +0.025 sorted) | now clears corrected bound (descriptive) |
| Q2 EARLY×high cell A | +0.171R, lb +0.109 | **+0.206R, lb +0.143** (hw 0.063) | SURVIVES, strengthens |
| Q4 rotation delta | +0.0004, lb −0.029 | −0.0115, lb −0.042 | RETIRE stands, more decisively |
| Q5 ladder A1 | +0.050, dLo1.25 −0.043 | +0.0612, dLo1.25 −0.0325 | NOT CERTIFIED stands |

Every Part-A verdict survives the vintage swap; the promotion cohort (bear×high) is
STRONGER under the current exit rule. Rule going forward: **do not mix D_dump reversal
cells with fresh-walk reversal cells** — treat R_stop_a/C_ceil default as the canonical
current-default reversal book for post-hoc cuts. (Known wobble: the seeded bootstrap is
ticker-insertion-order sensitive at the 3rd decimal; verdict-relevant signs are stable
under reordering.)

## Design implications

- **Continuation is done.** Every decomposition axis is falsified. The pre-authorized
  parking rule fires: a human-gated PR adds surfacing-only `surface_continuation` (default
  True → flipped False), continuation stays detected/scored/shadow-booked, and the
  registered cont_volband/extguard_tight forward books remain the formal arbiters.
- **Reversal exits are locally optimal on both tested axes**: full-retrace target (shipped
  2026-07-03, re-confirmed by the vintage shift being uniformly kinder) and the 0.25 stop
  buffer (Q7). The premium cohort thesis (bear×high, EARLY included) strengthened again.
- Remaining queue: Q9 tier redefinition (bear-conditioned per Q1), Q11 shadow-extras
  throughput, Q12 Alpaca paper drill; Q2 cell B + 0.10 repeats when a rev_combo re-run is
  worth scheduling.

---


# Appendix — Q6 ceiling sweep — full run report

# Q6 cont_ceiling_sweep_then_park -- ceiling_atr_mult sweep grade

Pinned 511-name corpus as-of 20260703; slippage 0.05 ATR baked into realized_r; house ticker-clustered bootstrap (seeded, canonical ticker order); continuation cohort only.

- loaded `C_ceil_lo`: 8 shards, 261258 rows, variants ['ceil_015', 'ceil_025', 'default']
- loaded `C_ceil_hi`: 8 shards, 261274 rows, variants ['ceil_045', 'ceil_055', 'ceil_065']
- loaded `D_dump`: 8 shards, 87090 rows, variants ['default']

## 1. Sanity anchor (gate)

C_ceil_lo `default` continuation book: n_signals=16813, filled=16273, missed=426, fill%=97.4%, n_closed=16221, clusters=511, expectancy=-0.1613R, clustered lb95=-0.1782R
- PASS: expectancy within +/-0.005 of -0.161R (-0.1613R)
- PASS: n_closed in [15900, 16500] (16221)
- PASS: clusters >= 500 (511)
- PASS: fill% in [95%, 99%] (97.4%)

Cross-walk identity vs D_dump default continuation (key = ticker+entry_date+opened_date, verified unique): 16813/16813 keys matched (100.00%); only-in-sweep=0, only-in-D_dump=0
- per-column mismatches on matched keys: {'entry_price': 0, 'stop': 0, 'target': 0, 'risk': 0, 'realized_r': 0, 'hold_bars': 0, 'fill_status': 0, 'status': 0, 'exit_reason': 0, 'exit_date': 0}
- PASS: trade sets identical (every column, realized_r to 1e-9)

**Anchor PASS** — the sweep walk reproduces the pinned book; cells are gradable.

## 2. Contamination check (reversal cohorts)

Baseline = C_ceil_lo `default` reversal book (70277 rows). Row-multiset identity over every dump column except `variant`:
- PASS: C_ceil_lo/ceil_015 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_ceil_lo/ceil_025 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_ceil_hi/ceil_045 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_ceil_hi/ceil_055 -- 70277 rows, 70277/70277 identical to baseline
- PASS: C_ceil_hi/ceil_065 -- 70277 rows, 70277/70277 identical to baseline

Within-sweep verdict: PASS -- all 6 ceiling values share a byte-identical reversal book; the ceiling knob provably does not touch reversal.

Cross-walk vs D_dump default reversal book: 70277/70277 keys matched; per-column mismatches: {'entry_price': 0, 'stop': 0, 'target': 70277, 'risk': 0, 'realized_r': 10739, 'hold_bars': 7835, 'fill_status': 0, 'status': 25, 'exit_reason': 4081, 'exit_date': 7847}
- FAIL: identical to D_dump
- Reading: NOT ceiling contamination (all 6 values are identical to each other, including both fresh walk invocations) -- the pre-existing D_dump reversal book is a different TARGET VINTAGE: same signals/entries/stops/fills, but `target` differs, moving exits. Flag for any sibling analysis that mixes D_dump reversal cells with fresh-walk reversal cells.

## 3. Main table -- continuation cohort per ceiling_atr_mult (slip 0.05)

fill% sits NEXT TO every expectancy (artifact watch: a tight-ceiling cell that 'wins' on a sliver of fills must be visible as such).

| ceiling | n_signals | filled | missed | inval | fill% | n_closed | clusters | exp R | lb95 (clust) | half-width | flags |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.15 | 16803 | 15514 | 1175 | 114 | 93.0% | 15467 | 511 | -0.103 | -0.120 | 0.017 | decisional |
| 0.25 | 16811 | 15984 | 713 | 114 | 95.7% | 15935 | 511 | -0.134 | -0.152 | 0.017 | decisional |
| 0.35 (default) | 16813 | 16273 | 426 | 114 | 97.4% | 16221 | 511 | -0.161 | -0.178 | 0.017 | decisional |
| 0.45 | 16814 | 16423 | 276 | 115 | 98.3% | 16368 | 511 | -0.183 | -0.200 | 0.017 | decisional |
| 0.55 | 16814 | 16501 | 198 | 115 | 98.8% | 16442 | 511 | -0.199 | -0.217 | 0.018 | decisional |
| 0.65 | 16815 | 16542 | 158 | 115 | 99.1% | 16481 | 511 | -0.213 | -0.231 | 0.018 | decisional |

## 4. Exit-reason mix per ceiling (closed-filled continuation trades)

The mechanism read: does a tighter ceiling change WHAT fills, or just how the same trades resolve?

| ceiling | n_closed | target% | stop% | momentum_flip% | time_stop% | avg hold (bars) |
|---|---|---|---|---|---|---|
| 0.15 | 15467 | 19.2% | 29.1% | 44.7% | 7.0% | 4.1 |
| 0.25 | 15935 | 16.7% | 28.9% | 46.4% | 8.0% | 4.3 |
| 0.35 | 16221 | 14.3% | 28.8% | 47.9% | 9.0% | 4.4 |
| 0.45 | 16368 | 12.5% | 28.7% | 48.9% | 9.9% | 4.5 |
| 0.55 | 16442 | 10.7% | 28.7% | 49.7% | 10.8% | 4.6 |
| 0.65 | 16481 | 9.1% | 28.7% | 50.4% | 11.8% | 4.6 |

## 5. Paired read vs default (what fills vs how much)

FILLED-set overlap on (ticker, entry_date): filled rows always carry entry_date == opened_date (the fill day) and are key-unique, so this is the clean fill identity. A missed booking re-anchors entry_date, so a filled<->missed flip is structurally unobservable by key -- the 'only default'/'only this' columns ARE the fills the ceiling adds/removes. Descriptive only; no paired bound is claimed.

| ceiling | both filled | filled only this | filled only default | fill-day shifts | entry-price diffs | both-closed pairs | paired mean dR (this - default) |
|---|---|---|---|---|---|---|---|
| 0.15 | 15514 | 0 | 759 | 0 | 11512 | 15464 | +0.0786R |
| 0.25 | 15984 | 0 | 289 | 0 | 10329 | 15932 | +0.0349R |
| 0.45 | 16273 | 150 | 0 | 0 | 8913 | 16221 | -0.0261R |
| 0.55 | 16273 | 228 | 0 | 0 | 8913 | 16218 | -0.0433R |
| 0.65 | 16273 | 269 | 0 | 0 | 8913 | 16216 | -0.0581R |

## 6. Pre-registered verdict (one-shot; no grid extension)

Decisional iff n_closed >= 20 and clusters >= 8 (half-width > 0.10R -> reported-but-non-decisional). NULL = no swept value's continuation cohort has clustered 95% lb > 0 net of 0.05 ATR.

- ceiling 0.15: lb=-0.120R (exp -0.103R, fill 93.0%, n=15467, cl=511, hw=0.017R) -> does not clear
- ceiling 0.25: lb=-0.152R (exp -0.134R, fill 95.7%, n=15935, cl=511, hw=0.017R) -> does not clear
- ceiling 0.35: lb=-0.178R (exp -0.161R, fill 97.4%, n=16221, cl=511, hw=0.017R) -> does not clear
- ceiling 0.45: lb=-0.200R (exp -0.183R, fill 98.3%, n=16368, cl=511, hw=0.017R) -> does not clear
- ceiling 0.55: lb=-0.217R (exp -0.199R, fill 98.8%, n=16442, cl=511, hw=0.018R) -> does not clear
- ceiling 0.65: lb=-0.231R (exp -0.213R, fill 99.1%, n=16481, cl=511, hw=0.018R) -> does not clear

**VERDICT: NULL** -- no swept ceiling's continuation cohort clears clustered 95% lb > 0 net of 0.05 ATR. The pre-authorized parking rule activates: a separate human-gated PR adds surfacing-only `surface_continuation: bool` (mirroring `reversal_surface_confirmed_only`; continuation stays detected/scored/shadow-booked; the registered cont_volband/extguard_tight books keep accruing as formal arbiters), flipped False. That PR is NOT part of this analysis. One-shot: the value grid is closed.

## 7. Caveats

- Replay-screened evidence tier only (pinned corpus, deterministic walk); nothing here is forward-confirmed.
- Only slip-0.05 dumps exist for the C walks; the pre-registered 0.10 robustness re-walk is conditional on POSITIVE and is moot under NULL.
- The pre-existing D_dump REVERSAL book is a different target vintage than the fresh walks (`target` differs on all 70,277 keyed rows; realized_r on 10,739): sibling analyses must not mix D_dump reversal cells with fresh-walk reversal cells. Continuation is unaffected (100% identity).
- The seeded clustered bootstrap is ticker-insertion-order sensitive at the 3rd decimal; rows are canonically sorted before every bound, and 3rd-decimal precision should not be over-read.


# Appendix — Q7 stop-width sweep — full run report

# Q7 rev_stop_width_sweep — results (2026-07-25)

**Question.** The MAE post-mortem showed 11.3% of reversal winners see MAE >= 0.8R — is the
0.25-ATR default stop clipping trades that were about to work? Sweep `stop_buffer_atr`
{0.25 default, 0.35, 0.50, 0.75} on `play_type == "reversal"`.

**Vehicle.** `replay_queue_experiments.py` walks `R_stop_a` (default + stop_035) and
`R_stop_b` (stop_050 + stop_075), shards s0..s7, pinned 511-name corpus (as-of 20260703),
0.05 ATR slippage baked into `realized_r`. Analysis script:
`scripts/replay_rev_stopwidth.py` (read-only over the dumps).

**VERDICT: FALSIFIED at 0.05 slippage — all three wider widths. No width comes close to the
promotion bar (clustered 95% delta lower bound vs default > 0); every delta *point* is already
negative. The 0.10 slippage re-walk is NOT needed. Record the falsification and stop; the
default 0.25 stays.**

---

## 1. Sanity: cross-walk identity of the default book

Expected scale ~44k closed reversal fills: **confirmed** (44,268 in `R_stop_a` default).

| comparison (closed reversal fills, key = ticker+opened_date) | L | R | matched | realized_r exact agree |
|---|---|---|---|---|
| R_stop_a default vs D_dump default | 44,268 | 44,293 | 44,268 (100.00% of L, 99.94% of R) | **75.80%** |
| R_stop_a default vs C_ceil_lo default (same-day control) | 44,268 | 44,268 | 44,268 (100.00% / 100.00%) | **100.00%** |

The naive D_dump identity check **fails on realized_r, for an identifiable, code-anchored
reason that is not a replay bug**:

- On the matched D_dump set, `entry_price` and `stop` agree **100.00%** but `target` agrees
  **0.00%**. D_dump was generated **2026-07-03** under the legacy
  `reversal_retrace_frac = 0.786`; the shipped default flipped to **1.0**
  (src/swing_screener/config.py:140, comment: "The legacy 0.786 stays measured forward")
  before the 2026-07-25 R_stop walks. Same entries, same stops, different targets →
  75.80% realized_r agreement (the 24.2% that disagree are trades whose exit interacted
  with the target) and mean realized_r +0.0432 (retrace 1.0) vs +0.0235 (legacy 0.786) on
  the matched set.
- The 25 D_dump-only closed trades are exactly the trades still **open** in the fresh walk
  (141 open vs 116) — the further 1.0-retrace targets leave them unresolved at walk end.
  Signal rows match exactly (70,277 both walks).
- Determinism itself is proven by the same-day control: `R_stop_a` default vs `C_ceil_lo`
  default (both 2026-07-25, both carry the base config) match **100.00%** on keys and on
  entry/stop/target/realized_r. The replay is deterministic across walks at fixed code+config.

Implication: all comparisons below use the **fresh** `R_stop_a` default as baseline (current
shipped config). D_dump-era numbers (e.g., the Q1 crosstab) are on the legacy-0.786 exit and
are not directly comparable in level.

## 2. Main table — per width, full reversal cohort

| variant | width | n_signal | fill% | n_closed | clusters | exp_R | ci_low | stop% | target% | time% | med_risk $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| default  | 0.25 | 70,277 | 64.5% | 44,268 | 511 | **+0.043** | **+0.027** | 52.2% | 15.0% | 32.8% | 3.540 |
| stop_035 | 0.35 | 70,277 | 64.5% | 44,260 | 511 | +0.040 | +0.025 | 49.5% | 15.3% | 35.2% | 3.833 |
| stop_050 | 0.50 | 70,277 | 64.5% | 44,250 | 511 | +0.039 | +0.025 | 45.5% | 15.8% | 38.7% | 4.268 |
| stop_075 | 0.75 | 70,277 | 64.5% | 44,241 | 511 | +0.038 | +0.025 | 39.5% | 16.3% | 44.3% | 4.987 |

(ci_low = house ticker-clustered 95% lower bound on expectancy; exit mix over closed fills.)

Note on trade sets: the stop knob sits below the entry floor, so the fill set turned out
**identical** across widths on this corpus (fill-set overlap vs default = 44,409/44,409 =
100.00% for all three widths; missed 24,460 and invalidated 1,286 identical too). The books
differ only in exits and in the R denominator — the tiny n_closed differences are end-of-walk
open positions. The comparison is still run per-cohort two-sample (the honest primitive), not
paired: realized_r lives in different denominators per width.

### 2b. Mechanism read (descriptive): what default stop-outs became

Matched closed keys where the default exited `stop`; each width's realized_r is in that
width's OWN (bigger) R:

| variant | n_matched | →stop | →target | →time_stop | meanR default | meanR width |
|---|---|---|---|---|---|---|
| stop_035 | 23,092 | 94.9% | 0.5% | 4.6% | −1.044 | −0.963 |
| stop_050 | 23,082 | 87.2% | 1.4% | 11.4% | −1.044 | −0.858 |
| stop_075 | 23,073 | 75.7% | 2.3% | 22.0% | −1.044 | −0.724 |

The hypothesis is **directionally real but economically empty**: wider stops do convert
stopouts (aggregate stop share 52.2% → 39.5% at 0.75), but the conversions land overwhelmingly
in **time_stops** (32.8% → 44.3%), not targets (15.0% → 16.3%). Even at 0.75, only 2.3% of
default stopouts reach target. And the per-R salvage is illusory per-dollar: −0.724 in a
1.41× denominator ≈ −1.02 default-R equivalents — the near-miss losers lose the same dollars,
while every winner's R shrinks with the bigger denominator.

## 3. Delta table — the decision numbers (width vs default, full cohort)

Two-sample clustered bootstrap (ticker clusters, sorted; house `clustered_two_sample_delta_low`),
NOT paired:

| variant | delta point | clustered 95% lb | n_w / clusters | n_def / clusters | verdict |
|---|---|---|---|---|---|
| stop_035 | −0.0033 | **−0.028** | 44,260 / 511 | 44,268 / 511 | FALSIFIED |
| stop_050 | −0.0044 | **−0.028** | 44,250 / 511 | 44,268 / 511 | FALSIFIED |
| stop_075 | −0.0055 | **−0.027** | 44,241 / 511 | 44,268 / 511 | FALSIFIED |

## 4. Tier cuts (pre-registered, descriptive)

### conviction_tier == premium (n_closed 989, clusters 423, all widths)

| variant | exp_R | ci_low | stop% | target% | time% | delta pt vs default | delta lb |
|---|---|---|---|---|---|---|---|
| default  | +0.103 | +0.019 | 43.6% | 10.7% | 45.7% | — | — |
| stop_035 | +0.092 | +0.011 | 41.1% | 10.9% | 48.0% | −0.0110 | −0.122 |
| stop_050 | +0.085 | +0.012 | 37.7% | 11.0% | 51.3% | −0.0177 | −0.124 |
| stop_075 | +0.087 | +0.020 | 32.4% | 11.6% | 56.0% | −0.0158 | −0.120 |

### conviction_tier == strong (n_closed ~16.8k, clusters 511)

| variant | exp_R | ci_low | stop% | target% | time% | delta pt vs default | delta lb |
|---|---|---|---|---|---|---|---|
| default  | +0.045 | +0.023 | 51.4% | 14.9% | 33.7% | — | — |
| stop_035 | +0.040 | +0.019 | 48.9% | 15.2% | 36.0% | −0.0044 | −0.036 |
| stop_050 | +0.039 | +0.020 | 44.9% | 15.6% | 39.5% | −0.0053 | −0.035 |
| stop_075 | +0.036 | +0.019 | 39.0% | 16.0% | 45.0% | −0.0089 | −0.037 |

### market_trend == bear (post-hoc-but-motivated; Q1's promotion axis; n_closed 11,791, clusters 511)

| variant | exp_R | ci_low | stop% | target% | time% | delta pt vs default | delta lb |
|---|---|---|---|---|---|---|---|
| default  | +0.155 | +0.125 | 49.2% | 11.7% | 39.1% | — | — |
| stop_035 | +0.147 | +0.117 | 46.7% | 11.8% | 41.4% | −0.0084 | −0.052 |
| stop_050 | +0.140 | +0.113 | 42.8% | 12.1% | 45.1% | −0.0152 | −0.059 |
| stop_075 | +0.127 | +0.104 | 37.3% | 12.3% | 50.4% | −0.0285 | −0.069 |

No cut rescues any width. The pattern is **monotone**: the wider the stop, the worse — and it
degrades *fastest* in bear, exactly where the reversal edge lives (bear delta point −0.0285 at
0.75, ~5× the full-cohort drag). Premium is the same story in level (+0.103 default is the best
of the four everywhere it matters).

## 5. R-denominator honesty

`realized_r` is denominated in each width's OWN risk — a wider stop is a bigger 1R, so per-R
comparisons already flatter wider stops on the loser side (a stopped trade loses ~−1R in a
bigger denominator = more dollars):

| variant | width | median risk $/share (closed fills) | × default |
|---|---|---|---|
| default  | 0.25 | 3.540 | 1.00× |
| stop_035 | 0.35 | 3.833 | 1.08× |
| stop_050 | 0.50 | 4.268 | 1.21× |
| stop_075 | 0.75 | 4.987 | 1.41× |

All conclusions above are **per-R, not per-dollar**. At equal per-trade dollar risk (the
sizing model), equal expectancy_r means equal dollars — so the per-R decline understates
nothing; wider is simply worse, and per-dollar the "salvage" of near-miss losers is ~zero
(section 2b).

## 6. Pre-registered stopping rule (queue doc Q7)

> Promote a width to stage 2 (forward variant behind a new reversal-specific knob) ONLY if its
> clustered 95% lower bound vs default > 0 net of 0.05 ATR slippage (n>=20, clusters>=8) AND
> the delta survives 0.10 slippage with upper bound > 0.

- stop_035: delta_lb = **−0.028** (n=44,260, cl=511) → FALSIFIED at 0.05
- stop_050: delta_lb = **−0.028** (n=44,250, cl=511) → FALSIFIED at 0.05
- stop_075: delta_lb = **−0.027** (n=44,241, cl=511) → FALSIFIED at 0.05
- **0.10 slippage re-walk: NOT needed** (it runs only on a 0.05 positive; there is none —
  every delta point is already < 0).

**Falsification recorded. `stop_buffer_atr = 0.25` stands for reversals; no reversal-specific
stop knob is warranted.** The MAE observation (11.3% of winners with MAE >= 0.8R) does not
translate into a capturable edge: the near-miss stopouts are mostly genuine losers that a wider
stop merely holds longer (→ time_stops), at the cost of shrinking every winner's R.

## Caveats

- D_dump is stale relative to the current config default (`reversal_retrace_frac` 0.786 → 1.0);
  the identity check passes on entries/stops/keys (99.94–100%) but not on targets/realized_r.
  Determinism was instead certified against the same-day C_ceil_lo default (100% agreement).
  Any future cross-walk identity checks should use a same-generation default book.
- Per-cohort two-sample deltas, not paired: with identical fill sets the two-sample bootstrap
  is conservative (entry noise is shared but not cancelled), so the lb's are slightly wide —
  irrelevant here since the *points* are negative.
- The clustered bootstrap is ticker-insertion-order sensitive at the 3rd decimal; tickers were
  sorted before clustering, but 3rd-decimal precision should not be over-read.
- Tier/regime cuts are descriptive (no multiplicity correction); the bear cut is
  post-hoc-but-motivated (Q1). None of them changes the decision.

## Repro

```
cd <worktree-root>
PYTHONPATH=src PYTHONIOENCODING=utf-8 <repo>/.venv/Scripts/python.exe \
    scripts/replay_rev_stopwidth.py --cache-root <repo>/.cache
python -m ruff check scripts/replay_rev_stopwidth.py   # passes
```


# Appendix — Part-A re-grade — full report

# Part-A re-grade: current-target reversal book vs recorded legacy values

Generated 2026-07-25 20:46 | corpus as-of 20260703 | slip 0.05 baked in | seeded house bootstraps (fully deterministic)

- legacy  book: 8 D_dump shards   -> 70277 reversal rows, 511 tickers (all variant=default)
- current book: 8 R_stop_a shards -> 70277 reversal rows (variant=default), 511 tickers
- legacy = D_dump (pre-flip reversal_retrace_frac 0.786 targets); current = R_stop_a variant=default (retrace 1.0 targets); entries/stops/fills shared

## Vintage effect: book agreement

```
matched (ticker, opened_date) pairs: 70277 (legacy 70277, current 70277)
  entry_price  differing rows: 0
  stop         differing rows: 0
  entry_date   differing rows: 0
  exit_reason  differing rows: 4081
  strength     differing rows: 0
  target       differing rows: 70277  (the retrace-frac flip)
  realized_r   identical rows: 59538 (84.72%); differing 10739 (15.28%)
  closed fills: legacy 44293, current 44268, both 44268
  mean R (own closed fills): legacy +0.0242, current +0.0432, shift +0.0190
  paired mean R shift (rows closed+filled in BOTH): +0.0197 on n=44268
    strength=early      mean R legacy +0.0202 -> current +0.0394 (shift +0.0193; paired +0.0199 on n=34571)
    strength=confirmed  mean R legacy +0.0387 -> current +0.0567 (shift +0.0180; paired +0.0189 on n=9697)
```

## Q1 -- rev_bear_highvol_crosstab key cells (closed fills, market_trend x volatility_tier)

Recorded (legacy book): bear x high exp +0.299  lb +0.218  lb_bonf6 +0.187  n=1225; bear x not-high pooled lb +0.080; bull x high lb -0.055; CONFIRMED bear x high raw lb +0.057  lb_bonf6 -0.003.

```
[LEGACY (replication)]
cohort                       exp R   ci_low lb_bonf6 n_closed clusters
bull x low                  -0.001   -0.040   -0.051     8517      376
bull x med                  -0.017   -0.042   -0.051    18135      501
bull x high                 +0.056   -0.055   -0.088      794      112
bear x low                  -0.129   -0.237   -0.270      718      179
bear x med                  +0.130   +0.098   +0.092     9848      500
bear x high                 +0.299   +0.218   +0.187     1225      206
bear x not-high (pooled)    +0.112   +0.080        -    10566      505
trend=bear (marginal)       +0.132   +0.104        -    11791      511
tier=high (marginal)        +0.159   +0.093        -     2347      219
CONFIRMED bear x high       +0.236   +0.057   -0.003      204       77

[CURRENT (re-grade)]
cohort                       exp R   ci_low lb_bonf6 n_closed clusters
bull x low                  +0.018   -0.024   -0.036     8512      376
bull x med                  +0.003   -0.022   -0.034    18115      501
bull x high                 +0.097   -0.022   -0.062      794      112
bear x low                  -0.089   -0.200   -0.234      718      179
bear x med                  +0.150   +0.118   +0.108     9848      500
bear x high                 +0.339   +0.255   +0.233     1225      206
bear x not-high (pooled)    +0.134   +0.101        -    10566      505
trend=bear (marginal)       +0.155   +0.127        -    11791      511
tier=high (marginal)        +0.195   +0.130        -     2347      219
CONFIRMED bear x high       +0.282   +0.087   +0.019      204       77

[verdict-sentence check on the CURRENT book]
  PASS  bear is the load-bearing axis (bear x high clears the house bar; bull x high does not)
  PASS  high-vol amplifies inside bear (bear x high exp AND lb above bear x not-high)
  PASS  bull x high fails (house lb <= 0)
  PASS  strongest cohort = bear x high (by house lb among clearing cohorts)
  PASS  bear x high survives its own Bonferroni-6 bound (lb_bonf6 > 0)
  strongest single cohort: bearxhigh
```

**Q1 verdict sentence SURVIVES on the current target rule.**

## Q2 -- early x high cell A (pre-registered bar: lb>0, n>=20, cl>=8, hw_low<=0.10)

Recorded (legacy book): exp +0.171  lb +0.109  n=1940  clusters=219  hw_low 0.063.

```
[LEGACY (replication)]
cell              role        exp R   ci_low  hw_low n_closed clusters
early|high        PRIMARY    +0.171   +0.109   0.063     1940      219
early|med         control    +0.015   -0.002   0.018    25064      505
early|low         control    -0.003   -0.038   0.036     7583      373
confirmed|high    control    +0.101   -0.023   0.124      407      113
confirmed|med     control    +0.040   +0.009   0.031     6898      503
  PASS  clustered 95% lb > 0         lb = +0.109
  PASS  n_closed >= 20               n_closed = 1940
  PASS  clusters >= 8                clusters = 219
  PASS  half-width <= 0.10R          hw_low = 0.063R

[CURRENT (re-grade)]
cell              role        exp R   ci_low  hw_low n_closed clusters
early|high        PRIMARY    +0.206   +0.143   0.063     1940      219
early|med         control    +0.032   +0.013   0.019    25050      505
early|low         control    +0.021   -0.016   0.038     7581      373
confirmed|high    control    +0.138   +0.008   0.130      407      113
confirmed|med     control    +0.060   +0.026   0.034     6892      503
  PASS  clustered 95% lb > 0         lb = +0.143
  PASS  n_closed >= 20               n_closed = 1940
  PASS  clusters >= 8                clusters = 219
  PASS  half-width <= 0.10R          hw_low = 0.063R

```

**Q2 cell A pre-registered bar STILL PASSES on the current book.**

## Q4 -- rotation tagged-vs-untagged delta (tag re-derived; entry-side, vintage-invariant)

Recorded (legacy book): point +0.0004  clustered lb -0.029 -> RETIRE.

```
sector map: 502 tickers; derived contexts: 70198 pairs

[LEGACY (replication)] rotation-tag share: 41.0% of all rows, 38.8% of closed fills (grade 1 bar: < 50%)
[LEGACY (replication)] tagged-vs-untagged on closed fills
side         exp R   ci_low n_closed clusters
tagged      +0.024   +0.002    17187      501
untagged    +0.024   +0.005    27106      511
delta (tagged - untagged): point +0.0004, clustered 95% lower bound -0.0290

[CURRENT (re-grade)] rotation-tag share: 41.0% of all rows, 38.8% of closed fills (grade 1 bar: < 50%)
[CURRENT (re-grade)] tagged-vs-untagged on closed fills
side         exp R   ci_low n_closed clusters
tagged      +0.036   +0.013    17171      501
untagged    +0.048   +0.029    27097      511
delta (tagged - untagged): point -0.0115, clustered 95% lower bound -0.0422

(1) discrimination: 41.0% < 50% -> PASS
(2) edge: delta_low -0.0422 > 0, n_tagged 17171 >= 20, clusters 501 >= 8 -> FAIL
```

**Q4 RETIRE verdict STANDS on the current book.**

## Q5 -- stamped conviction-ladder contrasts (K=4 one-sided Bonferroni; deciding bound = clustered 1.25th pct)

Recorded (legacy book): A1 premium-vs-rest delta +0.050  dLo1.25 -0.043; A2 premium+strong-vs-base point -0.0150 -> NOT CERTIFIED.
(Label note: the recorded A2 '-0.0150' reproduces as the A2 clustered dLo2.5 bound, not the point delta -- the legacy A2 point delta replicates as +0.0115.)

```
[LEGACY (replication)] stamped ladder
  premium  n_total=1412   n_closed=989    cl=423  exp=+0.073  lb2.5=-0.004
  strong   n_total=32638  n_closed=16814  cl=511  exp=+0.029  lb2.5=+0.006
  base     n_total=36227  n_closed=26490  cl=511  exp=+0.020  lb2.5=+0.004
A1 premium vs rest         hi: exp=+0.073 n=989 cl=423 | lo: exp=+0.023 n=43304 | delta=+0.0502 dLo2.5=-0.0265 dLo1.25=-0.0430 dLo0.625=-0.0525
A2 premium+strong vs base  hi: exp=+0.031 n=17803 cl=511 | lo: exp=+0.020 n=26490 | delta=+0.0115 dLo2.5=-0.0150 dLo1.25=-0.0195 dLo0.625=-0.0230
  B1 coverage: 70198/70277 rows matched a recomputed context (99.9%)
B1 premium' vs rest        hi: exp=+0.066 n=448 cl=295 | lo: exp=+0.024 n=43785 | delta=+0.0417 dLo2.5=-0.0541 dLo1.25=-0.0636 dLo0.625=-0.0699

[CURRENT (re-grade)] stamped ladder
  premium  n_total=1412   n_closed=989    cl=423  exp=+0.103  lb2.5=+0.022
  strong   n_total=32638  n_closed=16804  cl=511  exp=+0.045  lb2.5=+0.024
  base     n_total=36227  n_closed=26475  cl=511  exp=+0.040  lb2.5=+0.023
A1 premium vs rest         hi: exp=+0.103 n=989 cl=423 | lo: exp=+0.042 n=43279 | delta=+0.0612 dLo2.5=-0.0239 dLo1.25=-0.0325 dLo0.625=-0.0378
A2 premium+strong vs base  hi: exp=+0.048 n=17793 cl=511 | lo: exp=+0.040 n=26475 | delta=+0.0080 dLo2.5=-0.0187 dLo1.25=-0.0228 dLo0.625=-0.0258
  B1 coverage: 70198/70277 rows matched a recomputed context (99.9%)
B1 premium' vs rest        hi: exp=+0.108 n=448 cl=295 | lo: exp=+0.043 n=43760 | delta=+0.0652 dLo2.5=-0.0385 dLo1.25=-0.0535 dLo0.625=-0.0657

  (A) stamped   [A1]: dLo@1.25=-0.0325, top n=989, cl=423 -> NOT certified
  (B) challenger [B1]: dLo@1.25=-0.0535, top n=448, cl=295 -> NOT certified

```

**Q5 NOT CERTIFIED verdict STANDS on the current book.**

## Summary

| diagnostic | recorded (legacy book) | current-book verdict |
|---|---|---|
| Q1 crosstab | bear x high exp +0.299  lb +0.218  lb_bonf6 +0.187  n=1225 | verdict sentence SURVIVES |
| Q2 early x high A | exp +0.171  lb +0.109  n=1940  clusters=219  hw_low 0.063 | bar PASSES |
| Q4 rotation | point +0.0004  clustered lb -0.029 | RETIRE STANDS |
| Q5 tier ladder | A1 delta +0.050  dLo1.25 -0.043; A2 point -0.0150 | NOT CERTIFIED STANDS |
