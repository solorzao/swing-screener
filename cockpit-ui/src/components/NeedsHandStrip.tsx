import type { SettlementCard } from '../lib/api'
import type { ScreenId } from '../lib/screens'

/* The Needs-Your-Hand strip: pending items ARE lamps. `cards` is derived from
   the already-fetched forward-books payload (plan scope decision 8, no extra
   endpoint); `cards: null` means the payload hasn't arrived yet — that renders
   "…", never the empty state (an unknown must not read as "nothing needs your
   hand").

   Task 19 adds ONE more source: an UNREAD deep-analysis result. "Unread" is
   client-side (scope decision 14) — the App compares the permanent `attention`
   poll's `latest_analysis_id` against a localStorage last-seen id and passes the
   verdict here; the item navigates to the Analyst screen, where viewing it marks
   it seen. Task 20 grows this strip further (queued proposals, reflection due).
   The pending-decision cards are still the "…"-while-unknown source; the unread
   analysis is a separate, always-known client fact and never gates that "…". */

export function NeedsHandStrip({
  cards,
  analysisUnread = false,
  onNavigate,
}: {
  cards: SettlementCard[] | null
  /** True when attention.latest_analysis_id is newer than the localStorage
   * last-seen id — a finished analysis the user hasn't opened. */
  analysisUnread?: boolean
  /** Screen navigation for the unread-analysis item (→ Analyst). */
  onNavigate?: (id: ScreenId) => void
}) {
  const analysisItem =
    analysisUnread && onNavigate !== undefined ? (
      <button
        type="button"
        className="needs-hand-item needs-hand-analysis"
        title="a deep-analysis result you haven’t opened — click to review"
        onClick={() => onNavigate('analyst')}
      >
        new analysis ready · review
      </button>
    ) : null

  if (cards === null) {
    // Cards unknown, but the unread-analysis fact is client-side and always
    // known — surface it even while the books payload is still in flight.
    return (
      <div className="needs-hand">
        {analysisItem ?? <span className="needs-hand-empty">…</span>}
      </div>
    )
  }

  const pending = cards.filter(
    (c) =>
      c.state === 'settled-awaiting-decision' ||
      c.state === 'futile-awaiting-decision',
  )

  const nothing = pending.length === 0 && analysisItem === null

  return (
    <div className="needs-hand">
      {nothing ? (
        <span className="needs-hand-empty">nothing needs your hand</span>
      ) : (
        <>
          {pending.map((c) => (
            <span key={c.name} className="needs-hand-item">
              {c.name} ·{' '}
              {c.state === 'settled-awaiting-decision' ? 'settled' : 'futile'} —
              decide
            </span>
          ))}
          {analysisItem}
        </>
      )}
    </div>
  )
}
