import type { Facet } from '../lib/api'
import { Segmented } from './Segmented'

/* The GOLD/RESEARCH facet toggle (masthead) and its per-panel caption tag.
   Research is the default facet — the wide research grid (every screened
   candidate booked to the shadow book); gold is the would_surface-truthy
   slice (what production actually surfaced). Research carries the design's
   hatch cue as ONE small hatched tag next to each faceted panel's caption,
   not per-chip noise. The gold book is deliberately thin (stamping started
   ~2026-07), so gold panels caption that honestly. */

export function FacetToggle({
  facet,
  onFacet,
}: {
  facet: Facet
  onFacet: (facet: Facet) => void
}) {
  return (
    <Segmented
      options={[
        {
          value: 'gold',
          label: 'GOLD',
          title: 'what production actually surfaced (would_surface)',
        },
        {
          value: 'research',
          label: 'RESEARCH',
          title: 'the wide research grid, replay-graded',
        },
      ]}
      value={facet}
      onChange={onFacet}
    />
  )
}

/** The tag every faceted panel shows next to its caption. */
export function FacetCaption({ facet }: { facet: Facet }) {
  return facet === 'research' ? (
    <span
      className="facet-tag hatched"
      title="research facet — the wide research grid, replay-graded"
    >
      research
    </span>
  ) : (
    <span className="facet-tag">GOLD · thin until stamped history accrues</span>
  )
}
