import type { Facet } from '../lib/api'

/* The GOLD/RESEARCH facet toggle (masthead) and its per-panel caption tag.
   Research is the default facet — the wide would_surface grid, replay-graded —
   and carries the design's hatch cue as ONE small hatched tag next to each
   faceted panel's caption, not per-chip noise. The gold book is deliberately
   thin (stamping started ~2026-07), so gold panels caption that honestly. */

export function FacetToggle({
  facet,
  onFacet,
}: {
  facet: Facet
  onFacet: (facet: Facet) => void
}) {
  return (
    <span className="mh-seg">
      <button
        type="button"
        className={facet === 'gold' ? 'seg-on' : undefined}
        aria-pressed={facet === 'gold'}
        title="what production actually surfaced (would_surface)"
        onClick={() => onFacet('gold')}
      >
        GOLD
      </button>
      <button
        type="button"
        className={facet === 'research' ? 'seg-on' : undefined}
        aria-pressed={facet === 'research'}
        title="the wide research grid, replay-graded"
        onClick={() => onFacet('research')}
      >
        RESEARCH
      </button>
    </span>
  )
}

/** The tag every faceted panel shows next to its caption. */
export function FacetCaption({ facet }: { facet: Facet }) {
  return facet === 'research' ? (
    <span
      className="facet-tag hatched"
      title="research facet — the wide would_surface grid, replay-graded"
    >
      research
    </span>
  ) : (
    <span className="facet-tag">GOLD · thin until stamped history accrues</span>
  )
}
