import type { Facet, ForwardBooks, Polled } from '../lib/api'
import { FacetCaption } from './FacetToggle'
import { PanelBody } from './PanelBody'
import { SettlementCard } from './SettlementCard'

/** The Forward Books wall — one panel, shared verbatim by Mission Control's
 * center zone and the full-width `forward` screen (same data, same cards).
 * The poll itself lives in App (permanent roster — the Needs-Your-Hand strip
 * reads the same payload); this component only renders. */
export function ForwardBooksPanel({
  fb,
  facet,
  caption,
}: {
  fb: Polled<ForwardBooks>
  facet: Facet
  /** Extra panel-head caption (screen 4 states the settle-decision procedure
   * here; Mission Control omits it to keep the home zone compact). */
  caption?: string
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        FORWARD BOOKS <FacetCaption facet={facet} />
        {caption !== undefined && <span className="panel-caption">{caption}</span>}
      </div>
      <PanelBody polled={fb} noun="forward books">
        {(data) =>
          data.cards.length === 0 ? (
            <div className="panel-wait">no experiments registered</div>
          ) : (
            <div className="scard-wall">
              {data.cards.map((c) => (
                <SettlementCard key={c.name} card={c} />
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}
