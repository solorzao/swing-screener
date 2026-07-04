# 2026-07-04 studies — highvol revalidated, spring retired, limit-entry rescue dead, edge survives its fill bias

Three pre-registered measurement studies from the 2026-07-04 deep review, all on the
PINNED corpus (as-of `20260703`: 502 tickers at that vintage + 9 at `20260628` + 1 at
`20260623` — the same at-or-before resolution the 2026-07-03 queued-experiment batch
used), net of ATR slippage, ticker-clustered 2.5th-pct lower bounds via
`analytics.performance.breakdown`. Studies 1 and 3 are pure analysis of the
`replay_queue_experiments.py` dumps (no new replay); study 2 is a fresh 16-shard walk.

Sanity anchor: the default reversal book reproduces the 2026-07-03 numbers exactly
(full +0.043 / lb +0.027; confirmed +0.057 / lb +0.028 at 0.05), so all reads below are
apples-to-apples with `2026-07-03-queued-experiments.md`.

## 1. Revalidation: rev_highvol / spring / highvol+spring under current defaults

The three strongest pre-refresh claims (+0.157R highvol, +0.141R spring, +0.25R stack —
all measured before the corpus refresh that moved legacy CONFIRMED +0.125R→+0.004R)
re-run on the pinned corpus under the shipped defaults (retrace 1.0, confirm_window 3,
fill_window 5). `scripts/replay_rev_combo.py --as-of 20260703 --shard i/8 --out ...`
then `--aggregate`; n=88,356 reversal rows, 511 tickers.

| arm | cohort | @0.05 exp / lb | @0.10 exp / lb | n closed | clusters |
|---|---|---|---|---|---|
| default | full | +0.043 / **+0.027** | +0.014 / −0.002 | 44,268 | 511 |
| default | confirmed | +0.057 / **+0.028** | +0.027 / −0.002 | 9,697 | 511 |
| **highvol** | full | +0.072 / **+0.038** | +0.048 / **+0.014** | 6,732 | 507 |
| highvol | confirmed | +0.082 / **+0.010** | +0.057 / −0.015 | 1,308 | 455 |
| spring | full | +0.002 / −0.042 | −0.025 / −0.069 | 3,871 | 505 |
| spring | confirmed | −0.036 / −0.213 | −0.067 / −0.245 | 261 | 200 |
| highvol_spring | full | +0.103 / +0.023 | +0.082 / +0.001 | 989 | 423 |
| highvol_spring | confirmed | +0.219 / −0.208 | +0.191 / −0.236 | 51 | 49 |

**Verdicts:**

- **rev_highvol REVALIDATED — and it is the only cohort measured in this project that
  clears the clustered bound at BOTH cost levels** (even the shipped default book goes
  lb −0.002 at 0.10). Point estimate roughly halved vs the pre-refresh claim, but the
  bound holds with real sample (n=6,732). The `rev_highvol` forward variant keeps its
  roster slot; its shadow book remains the promotion arbiter.
- **spring RETIRED as a gate.** The +0.141R claim does not reproduce (full ~0, confirmed
  outright negative). Corpus shift, not noise: 505 clusters at n=3,871.
- **highvol+spring stack: no demonstrable increment over highvol alone.** Its lower
  bound is WORSE than highvol's at both cost levels; the confirmed∩stack cell (n=51) is
  noise. The +0.25R claim was a 250-name-subset artifact, as the full-universe rule
  ("always confirm at 503+") predicted.
- **Consequence for the conviction tiers:** `config.py`'s premium tier is defined as
  highvol+spring (weight 2.0). The evidence now says the spring half contributes nothing
  — the premium tier should be redefined as highvol-only. NOT changed in this PR:
  a tier redefinition is a surfacing/measurement change that should ride its own
  reviewed change, and forward confirmation still belongs to the `rev_highvol` book.

## 2. MAE post-mortem: the continuation limit-entry rescue is counterfactually dead

`scripts/replay_mae_postmortem.py` over the `D_dump` shards (16,221 continuation
closed+filled trades, book −0.161R; `low_water`/`high_water` populated by the shared
advance stepper). The 2026-07-03 rank sweep left entry PRICE as the last untested
continuation lever; the hypothesis was that a confirm-then-rest-a-limit entry (the
mechanic that IS the reversal edge) rescues the book.

**It does not.** The counterfactual resting-limit book (filled cohort re-priced as
`(R+x)/(1−x)`, misses = no trade) is WORSE than baseline at every depth:

| limit depth x (R) | fill% | winners captured | cf exp R | cf lb |
|---|---|---|---|---|
| 0.05 | 91% | 72% | −0.253 | −0.270 |
| 0.20 | 85% | 55% | −0.214 | −0.236 |
| 0.30 | 79% | 42% | −0.202 | −0.225 |
| 0.40 | 73% | 30% | −0.196 | −0.221 |
| 0.65 | 54% | 10% | −0.236 | −0.272 |

Mechanism: **adverse selection dominates the price improvement.** Winners barely
retrace (median MAE 0.24R) while losers retrace deeply (median 0.91R) — a resting limit
preferentially fills the failures and misses the runners. In continuation, post-signal
weakness IS the failure signal; the liquidity-provision logic that pays reversal for
buying weakness runs the other way here. Caveats (why "counterfactually" dead, not
replay-dead): exit timing not re-simulated, fill-bar low excluded (capture understated),
same-bar bias unchanged — none of which plausibly closes a ~0.2R gap against the grain
of the selection effect. Combined with the settled falsifications (ordering, gates,
timing, regime), the entry-price family is dead without building the pending-limit
replay: **the continuation parking rule can be pre-registered on this evidence**, with
only the `ceiling_atr_mult` sweep left as a cheap final decomposition.

Side-findings queued as future arms/questions:

- Reversal winners nearly stop 3× more than continuation winners (MAE ≥ 0.8R: 11.3% vs
  3.7%) — a real `stop_buffer` case for a reversal stop-width arm.
- 21.6% of reversal trades that touch +1R close at ≤0 (continuation: 14.2%) — the
  `be_1r` breakeven arm is chasing a real phenomenon; its paired-arm delta decides.
- Full-retrace target is reached by 15.1% of reversal fills (vs 43.5% at retrace 0.5) —
  consistent with the shipped geometry sweep: rarer but bigger wins.

## 3. Same-bar fill+stop bracket: the reversal edge is not a simulation artifact

`scripts/replay_fill_stop_bracket.py` over the `R_target` retrace_100 dumps (the shipped
default geometry). `resolve_fill`'s documented accepted bias — a bar that sweeps both
the resting limit and the stop books as "filled", stop first checked next bar — is
worst-shaped for reversal and had never been measured.

- 6.27% of reversal fills touched the original stop on their fill bar; **88% of those
  stopped out anyway** (flagged cohort's actual mean −0.729R vs the −1.044R re-settle).
- Re-settling every flagged trade at the book's mean stop-exit R (the fully pessimistic
  end; truth lies inside the bracket):

| cohort @0.05 | optimistic exp / lb | pessimistic exp / lb |
|---|---|---|
| full book | +0.043 / +0.027 | +0.024 / **+0.007** |
| confirmed | +0.057 / +0.029 | +0.037 / **+0.009** |

**The bracket does not straddle zero at 0.05 slippage** — the confirmed edge survives
its own worst-case fill-sequencing assumption. At 0.10 both ends are underwater, which
is the already-documented cost fragility, not new information. The bias is now priced
(~0.02R of book expectancy) rather than merely disclosed.

## Reproduction

```bash
# study 1 + 3 (pure analysis of existing dumps):
python scripts/replay_mae_postmortem.py
python scripts/replay_fill_stop_bracket.py
# study 2 (16 shard walks, ~25 min each wave of 8):
for slip in 0.05 0.10; do for i in 0 1 2 3 4 5 6 7; do
  python scripts/replay_rev_combo.py --as-of 20260703 --slippage $slip \
    --shard $i/8 --out .cache/rev_combo &
done; wait; done
python scripts/replay_rev_combo.py --aggregate .cache/rev_combo
```

Playbook (`edge/*.md`) updates are deliberately NOT hand-applied here — the 2026-07-03
md/verdicts drift showed the hand-edit channel bypasses the machine record; these
verdicts should enter the playbooks through the reflection cycle referencing this doc.
