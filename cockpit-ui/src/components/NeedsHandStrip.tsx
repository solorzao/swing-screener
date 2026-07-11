import type { SettlementCard } from '../lib/api'

/* The Needs-Your-Hand strip: pending decisions ARE lamps. Client-side only —
   derived from the already-fetched forward-books payload, no extra endpoint
   (plan scope decision 8). `cards: null` means the payload hasn't arrived yet;
   that renders "…", never the empty state — an unknown must not read as
   "nothing needs your hand". */

export function NeedsHandStrip({ cards }: { cards: SettlementCard[] | null }) {
  if (cards === null) {
    return (
      <div className="needs-hand">
        <span className="needs-hand-empty">…</span>
      </div>
    )
  }
  const pending = cards.filter(
    (c) =>
      c.state === 'settled-awaiting-decision' ||
      c.state === 'futile-awaiting-decision',
  )
  return (
    <div className="needs-hand">
      {pending.length === 0 ? (
        <span className="needs-hand-empty">nothing needs your hand</span>
      ) : (
        pending.map((c) => (
          <span key={c.name} className="needs-hand-item">
            {c.name} ·{' '}
            {c.state === 'settled-awaiting-decision' ? 'settled' : 'futile'} —
            decide
          </span>
        ))
      )}
    </div>
  )
}
