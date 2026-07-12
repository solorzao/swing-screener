import type { PositionBadge } from '../lib/api'

/* The get-out lamp shared by the RiskStrip (Zone B), the positions table, and
   Task 17's PickCard actionability cell. ONE definition of the swatch + its
   screen-reader label so the two Task-16 surfaces never drift apart: the
   colored swatch is aria-hidden (shape redundancy lives in .plamp-* CSS —
   green circle / amber triangle / red square / dashed hollow), and a .vh span
   carries the same text to a screen reader, so the state is never title-only
   silence. `red` price ≤ stop, `yellow` ≥ target, `green` between, `unknown`
   no quote. UNKNOWN is never green. */

const BADGE_TITLE: Record<PositionBadge, string> = {
  red: 'price at/under the stop',
  yellow: 'price at/over the target',
  green: 'between stop and target',
  unknown: 'no quote — state unknown',
}

export function PositionLamp({ badge }: { badge: PositionBadge }) {
  return (
    <>
      <span
        className={`plamp plamp-${badge}`}
        title={BADGE_TITLE[badge]}
        aria-hidden="true"
      />
      <span className="vh">{BADGE_TITLE[badge]}</span>
    </>
  )
}
