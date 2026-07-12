import type { PositionBadge } from '../lib/api'
import { Lamp } from './Lamp'

/* The get-out lamp for the RiskStrip (Zone B) and the positions table: it binds
   the position-badge vocabulary to the shared Lamp primitive (the swatch + .vh
   dual-render — see Lamp.tsx; PickCard's ActionabilityLamp binds its own). `red`
   price ≤ stop, `yellow` ≥ target, `green` between, `unknown` no quote.
   PositionBadge is exactly Lamp's color set, so the badge is the color. */

const BADGE_TITLE: Record<PositionBadge, string> = {
  red: 'price at/under the stop',
  yellow: 'price at/over the target',
  green: 'between stop and target',
  unknown: 'no quote — state unknown',
}

export function PositionLamp({ badge }: { badge: PositionBadge }) {
  return <Lamp color={badge} title={BADGE_TITLE[badge]} />
}
