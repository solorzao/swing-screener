import type { VerdictTier } from '../lib/api'
import { Lamp } from './Lamp'
import type { LampColor } from './Lamp'

/* TierChip — a verdict row's tier as the shared Lamp plus its word (Task 18).
   Tier rides the verdict wire rows, NEVER a Stat (the Stat key set is closed,
   scope decision 11), so this reads `VerdictTier` directly.

   The color doctrine (North Star, scope decision 11): saturated green is the
   app's FIRST honest edge claim and belongs to `forward_confirmed` ALONE —
   cleared the corrected lower bound on the LIVE forward shadow book. Everything
   less is honestly less: `replay_screened` is amber (a backtest screen, not
   live-confirmed) and `hunch` is GRAY (unproven — an idea, not an edge). Gray
   is the new LampColor member (Lamp's doc note: extend, don't re-implement) with
   its own `.plamp-gray` swatch (a solid ring — inert, distinct from unknown's
   dashed ring and green's filled disc), so the "never hue alone" redundancy
   holds. UNKNOWN is never green: a hand-edited sidecar carrying an unrecognized
   tier renders the dashed unknown lamp, never a fabricated confirmation. */

interface TierMeta {
  color: LampColor
  label: string
  title: string
}

const TIER: Record<VerdictTier, TierMeta> = {
  forward_confirmed: {
    color: 'green',
    label: 'forward-confirmed',
    title:
      'forward-confirmed — cleared the corrected lower bound on the LIVE forward shadow book; the only saturated-green claim in the app',
  },
  replay_screened: {
    color: 'yellow',
    label: 'replay-screened',
    title:
      'replay-screened — cleared the bound on the haircut REPLAY corpus only; a backtest screen, NOT live-confirmed',
  },
  hunch: {
    color: 'gray',
    label: 'hunch',
    title:
      'hunch — has not cleared the corrected lower bound on either book; an idea, not an edge',
  },
}

export function TierChip({ tier }: { tier: VerdictTier }) {
  // The wire passes `tier` through verbatim (a hand-edited sidecar could carry
  // anything), so the lookup is defensively widened — an unknown tier is UNKNOWN,
  // never assumed confirmed.
  const meta = (TIER as Record<string, TierMeta>)[tier]
  if (meta === undefined) {
    return (
      <span className="tchip tchip-unknown">
        <Lamp
          color="unknown"
          title={`unrecognized tier "${String(tier)}" — never assumed confirmed`}
        />
        <span className="tchip-label">{String(tier)}</span>
      </span>
    )
  }
  return (
    <span className={`tchip tchip-${tier}`}>
      <Lamp color={meta.color} title={meta.title} />
      <span className="tchip-label">{meta.label}</span>
    </span>
  )
}
