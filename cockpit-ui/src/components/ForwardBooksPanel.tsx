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
}: {
  fb: Polled<ForwardBooks>
  facet: Facet
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        FORWARD BOOKS <FacetCaption facet={facet} />
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
