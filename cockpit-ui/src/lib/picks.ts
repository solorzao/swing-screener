/* Pick-surface helpers that are NOT components — they live here, not in
   PickCard.tsx, so the component files export only components (the
   only-export-components lint rule). */

import type { CohortRow, Stat } from './api'

/** The client-side cohorts join for a pick's cohort ref `(play_type, strength)`
 * → the tier's measured-edge Stat (design's ZONE D "the tier's measured edge, not
 * the pick's score"). Prefers the per-strength split, then falls back to the
 * play-type aggregate row; null when neither exists (no closed trades on this
 * facet yet — the caller simply omits the chip).
 *
 * This mirrors the aggregation in
 * src/swing_screener/cockpit/routers/books.py::cohort_stats — play_type-major,
 * an aggregate row with `strength: null`, and per-strength splits whose key is
 * `str()`-coerced (a null strength becomes the sentinel string "None", distinct
 * from the aggregate's real `null`). So a pick whose own strength is null
 * matches the "None" split first, then the aggregate — never another play
 * type's rows. (Follow-up: this join would be more robust as a server-resolved
 * `cohort_stat: Stat | null` field on the pick row — see the commit body.) */
export function resolveCohortStat(
  cohorts: CohortRow[],
  ref: { play_type: string; strength: string | null },
): Stat | null {
  const wantStrength = ref.strength === null ? 'None' : ref.strength
  const split = cohorts.find(
    (c) => c.key === ref.play_type && c.strength !== null && c.strength === wantStrength,
  )
  if (split !== undefined) return split.stat
  const aggregate = cohorts.find((c) => c.key === ref.play_type && c.strength === null)
  return aggregate !== undefined ? aggregate.stat : null
}
