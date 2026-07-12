# Settle bar recalibrated 0.15R → 0.10R — decision + accrual watch (2026-07-12)

**Decision (shipped, PR #113, merge commit `dccad78`):** tightened `target_ci_halfwidth_r`
on all 9 pre-registered experiments from the placeholder seed **0.15R** to the reviewed
value **0.10R**, now equal to `mde_r`. `mde_r` (the 0.10R futility floor) is unchanged.

## Why

With the settle bar (0.15R) *wider* than the effect size it tests (`mde_r` 0.10R), a
"settled" verdict could land on a confidence interval wider than the edge itself — so
"settled" did not guarantee you could tell a real edge from a dud. Setting the bar equal
to `mde_r` fixes that: a settled interval is now no wider than the effect it measures.

The 0.10R line is what the reversal book supports — genuine candidates sit at 0.20R+
headline (bear-regime +0.20R, high-vol +0.24R) while duds cluster ≤0.06R, so 0.10R
cleanly separates the two.

Both the machine field **and** the verbatim `stopping_rule` prose were changed together,
so the settlement card never displays a bound the engine doesn't use. Re-registered
2026-07-12 (`registered_sha` → the recalibration commit `c2662eb`, which carries the
0.10R rule verbatim); each experiment's original June/July registration date and sha are
preserved in its provenance note. Merged with a merge commit, not squash, so `c2662eb`
persists for `registered_sha` to resolve.

## Watch item — forward-accrual drag

The tighter bar roughly **doubles** the closes each experiment needs before it settles
(n scales ~ 1/halfwidth²). On the thin forward book (~weekly cadence) that is real
calendar time — nothing settles faster, just more trustworthily.

- **The signal is already in the cockpit.** Forward Books settlement cards show
  `n_needed` and `eta` (eta from the trailing-30-day accrual rate). The engine won't
  project until an experiment has ≥5 accrued closes, so before then the ETAs are noise.
- **Checkpoint: ≈ 2026-09** — once the first experiments have ~30 forward trading days of
  closes accrued (also when the engine's ETA projection first becomes meaningful), or the
  first time a settlement card shows a non-null `n_needed`. Glance at the ETAs. Months of
  wait is fine; years means the bar is too tight for the trade cadence.
- **Escape hatch:** if ETAs are unacceptably far out, nudge **both** `mde_r` and
  `target_ci_halfwidth_r` to **0.125R** and re-register (same edit as this recalibration).
  Loses almost nothing on detection, settles ~40% sooner (~1.4× today's sample cost vs
  ~2.25× at 0.10R).
