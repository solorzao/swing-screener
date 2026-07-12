import type { Facet, ForwardBooks, Polled } from '../lib/api'
import { ForwardBooksPanel } from '../components/ForwardBooksPanel'

/** Screen 4: the Forward Books wall, full-width (same panel as Mission
 * Control's center zone — more columns, same cards, same App-level poll). */
export function ForwardScreen({
  fb,
  facet,
}: {
  fb: Polled<ForwardBooks>
  facet: Facet
}) {
  return (
    <main className="grid-single">
      <ForwardBooksPanel fb={fb} facet={facet} />
    </main>
  )
}
