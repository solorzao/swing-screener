# Reversal rotation capture — root cause of the 2026-07-01/02 miss + the windowed-confirmation fix

**Trigger.** A violent two-day rotation into software (IGV +10.4% over 5 sessions while XLK fell
2.2% and SPY was flat; WDAY +10.6%, PTC +9.6%, NOW +7.1%, CRM +6.0% in two days) produced zero
reversal picks in the digest. This doc records the diagnosis, the experiments, and what shipped.

## Root cause — the rotation WAS detected; four stacked layers hid it

Replaying the screen on real Jul 1–2 bars (full universe, per-gate funnel):

1. **Thrust day (Jul 1):** 51 EARLY signals fired, including CRM (0.425), WDAY (0.435), INTU,
   SNPS, CDNS, PTC. `reversal_surface_confirmed_only=True` hides EARLY; the 7 confirmed that
   day were non-software. Digest showed none of the rotation.
2. **Confirmation day (Jul 2):** CRM (0.541), WDAY (0.552), PTC (0.497) confirmed — among 31
   same-day confirmations. The score-ranked top-5 (no sector logic) crowded them out at ranks
   9–12. INTU missed confirmation by $0.80 (close 275.35 vs flip high 276.15) and, under the
   next-bar-only rule, the setup died permanently.
3. **Already-ran filter:** confirmation *requires* a close above the flip high while the entry
   ceiling sits 38.2% below it, so every confirmed reversal reads "extended" at digest time and
   `digest_drop_already_ran` deleted whatever survived.
4. **Fill mechanics:** every Jul-1 signal's pullback-limit ceiling sat below Jul 2's low — a
   V-move never retraces into the band, so nothing fills (the published liquidity-provision
   literature says the short-term reversal premium accrues overnight; a below-market resting
   limit anti-selects V-bottoms).

## Experiments (full 511-name / 5y corpus through 2026-07-02, net of 0.05 ATR slippage,
ticker-clustered 95% bootstrap bounds, reversal book only, baseline arm)

Three default-off detector/zone knobs were added so screen variants could race the mechanics
(`reversal_confirm_window`, `reversal_anchor_confirmation`, `reversal_ceiling_at_close`;
legacy equivalence at defaults proven over 594,479 bar-decisions / 61,999 signals, 0 mismatches).
Tournament: `scripts/replay_rotation_entry.py` (sharded across cores; each shard keeps its own
small throwaway book — the single shared SQLite grows quadratically slow past ~300 names).

| variant (confirmed cohort) | exp R | 95% low | fill | closed | clusters |
|---|---|---|---|---|---|
| default (next-bar confirm, pullback limit) | +0.004 | -0.028 | 44% | 6,515 | 511 |
| **confirm3** (`reversal_confirm_window=3`) | **+0.039** | **+0.012** | 43% | 9,706 | 511 |
| confirm3_anchor | +0.003 | -0.014 | 60% | 13,541 | 511 |
| anchor_confirm | -0.022 | -0.043 | 61% | 9,059 | 511 |
| chase_close (ceiling at trigger close) | -0.062 | -0.075 | 92% | 13,657 | 511 |
| chase_anchor | -0.061 | -0.074 | 92% | 13,633 | 511 |

- **Chasing is refuted.** Fill rate roughly doubles and expectancy collapses. Buying the
  pullback IS the edge; the fix for V-rotations is not paying up.
- **The confirmation window is the fix.** `confirm3` admits flips that confirm 2–3 bars late
  (trailing HA-green run, first close above the flip high, decline gates measured as-of the
  flip). It emits +53% more confirmed signals and clears the screened bar while legacy default
  does not on this corpus.
- **The late-confirm increment alone** (the cohort legacy can never see): **+0.110R, clustered
  lower bound +0.065R, n=7,833 (3,191 closed, 507 clusters)** — the strongest cohort measured
  on this book. A pause bar leaves the limit near the market (fills happen) and a digested
  confirmation beats a vertical one. Paired confirm3-vs-default delta: +0.035R (clustered delta
  lower bound -0.003 — the increment test above is the sharper read).
- **Cost robustness at 0.10 ATR slippage:** see the results table appended below.
- Rotation-day attribution (>=4 same-sector same-day signals) tags ~85% of the book — reversal
  signals inherently cluster; the crowding fix belongs in surfacing (sector cap), not the gate.

## Shipped

1. **Surfacing** (the miss mechanism, independent of the detector):
   - play-type-aware already-ran filter — "extended" reversals are a working resting limit and
     are kept; only a broken stop drops them;
   - sector cap (`reversal_max_per_sector=2`) with a 20-deep pool so the actionability drop and
     the cap backfill the top-5 instead of shrinking it;
   - `premium_only`/`confirmed_only` compose (AND) instead of the premium elif override;
   - stage-attributed funnel line: detected · confirmed · fresh · actionable · surfaced.
2. **Detector:** `reversal_confirm_window` default 1 -> 3 (the replay-screened candidate); the
   legacy rule stays measured forward as the `rev_confirm1` screen variant, so promotion/demotion
   follows the normal reflection path on live shadow data.
3. **Shadow book:** `resolve_pending` takes per-variant fill windows (expiry used to clamp every
   variant to the base config's window, corrupting window A/Bs).
4. **Edge hygiene:** withdrew the three queued proposed variants whose deltas only touch
   continuation-path knobs (no-ops on the reversal book that would have minted false nulls);
   recorded the screened candidate, the two refuted mechanics, and the corpus-shift caveats in
   `edge/reversal.md`.

## Not shipped (deliberately)

- EARLY surfacing stays off: EARLY graded +0.020R (lb +0.005) on this corpus — thin, and the
  strong sub-cohorts came from a wide sweep; left as hunches for the reflection loop.
- No chase/anchor entry modes: refuted (kept as default-off knobs for future replay work).
- No sector-thrust detection gate: the >=4-cluster tag doesn't discriminate; a sector-relative
  definition is queued as an open question.

## Robustness appendix (filled after the 0.10-slippage walk)

_TBD_
