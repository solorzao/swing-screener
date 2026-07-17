import type { Facet, ForwardBooks, Polled } from '../lib/api'
import { ForwardBooksPanel } from '../components/ForwardBooksPanel'
import { OpenBookPanel } from '../components/OpenBookPanel'

/** Screen 4: the open book over the settlement wall — the browsable table of
 * currently-RUNNING paper trades, then the aggregate experiment cards they feed
 * (same panel as Mission Control's center zone, same App-level poll). The
 * wall's caption states the out-of-app half of a settlement decision at the
 * point of decision (the cards' stopping rules are the in-app half). */
export function ForwardScreen({
  fb,
  facet,
  wake,
}: {
  fb: Polled<ForwardBooks>
  facet: Facet
  wake: number
}) {
  return (
    <main className="grid-single">
      <OpenBookPanel facet={facet} wake={wake} />
      <ForwardBooksPanel
        fb={fb}
        facet={facet}
        caption="an “awaiting decision” card settles by a human PR: delete its roster line (arms.py / variants.py), mark its edge/experiments.json row retired with decided_at + decision — one commit, audit record kept"
      />
    </main>
  )
}
