import type { Attention, SettlementCard } from '../lib/api'
import type { ScreenId } from '../lib/screens'

/* The Needs-Your-Hand strip: pending items ARE lamps. It merges TWO kinds of
   source, deliberately kept separate:

   1. The forward-books settlement cards (`cards`, derived from the already-fetched
      forward-books payload — plan scope decision 8, no extra endpoint). These are
      the "…"-while-unknown source: `cards: null` means the payload hasn't arrived
      (or errored, force-nulled by App), which must render "…", never the empty
      state — an unknown must not read as "nothing needs your hand".

   2. The permanent `/api/attention` poll's client-known facts (Task 20 completes
      this roster): queued proposals ("· decide"), approved-pending-promotion
      ("· promote" — an approval MARKS, a human promotes, so it stays until the
      promotion commit lands), reflection-due play types, and the unread deep
      analysis (Task 19 — "unread" is a client-side localStorage compare done in
      App and passed as a bool). These are ALWAYS-known facts (last-good attention
      is fine for a low-stakes nudge) and never gate the cards' "…".

   Every attention/analysis item is a button that navigates to where it is acted
   on: proposals + reflection → Playbooks; unread analysis → Analyst. */

export function NeedsHandStrip({
  cards,
  attention = null,
  analysisUnread = false,
  onNavigate,
}: {
  cards: SettlementCard[] | null
  /** The permanent attention poll's last-good payload (proposals / reflection).
   * null when unavailable — those items simply don't render (a low-stakes feed). */
  attention?: Attention | null
  /** True when attention.latest_analysis_id is newer than the localStorage
   * last-seen id — a finished analysis the user hasn't opened. */
  analysisUnread?: boolean
  /** Screen navigation for the actionable items. */
  onNavigate?: (id: ScreenId) => void
}) {
  const nav = (id: ScreenId) => () => onNavigate?.(id)

  // The attention-sourced items (always-known client facts, independent of the
  // cards' "…"). Each navigates to where it is decided.
  const attentionItems =
    onNavigate === undefined || attention === null
      ? []
      : [
          ...attention.proposals_queued.map((name) => (
            <button
              key={`q:${name}`}
              type="button"
              className="needs-hand-item needs-hand-btn"
              title="a queued proposal awaiting your decision — click to review in Playbooks"
              onClick={nav('playbooks')}
            >
              {name} · decide
            </button>
          )),
          ...attention.proposals_approved_pending.map((name) => (
            <button
              key={`a:${name}`}
              type="button"
              className="needs-hand-item needs-hand-btn"
              title="approved but not yet promoted — promotion is your 3-file commit; click to review in Playbooks"
              onClick={nav('playbooks')}
            >
              {name} · promote
            </button>
          )),
          ...attention.reflection_due.map((pt) => (
            <button
              key={`r:${pt}`}
              type="button"
              className="needs-hand-item needs-hand-btn"
              title="this play type's forward book re-armed a reflection — click to review in Playbooks"
              onClick={nav('playbooks')}
            >
              {pt} reflection due
            </button>
          )),
        ]

  const analysisItem =
    analysisUnread && onNavigate !== undefined ? (
      <button
        key="analysis"
        type="button"
        className="needs-hand-item needs-hand-analysis"
        title="a deep-analysis result you haven’t opened — click to review"
        onClick={nav('analyst')}
      >
        new analysis ready · review
      </button>
    ) : null

  const clientItems = [...attentionItems, ...(analysisItem !== null ? [analysisItem] : [])]

  if (cards === null) {
    // Cards unknown, but the attention/analysis facts are client-side and always
    // known — surface them even while the books payload is still in flight; only
    // when NOTHING at all is known does the strip fall back to "…".
    return (
      <div className="needs-hand">
        {clientItems.length > 0 ? clientItems : <span className="needs-hand-empty">…</span>}
      </div>
    )
  }

  const pending = cards.filter(
    (c) =>
      c.state === 'settled-awaiting-decision' ||
      c.state === 'futile-awaiting-decision',
  )

  const nothing = pending.length === 0 && clientItems.length === 0

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
          {clientItems}
        </>
      )}
    </div>
  )
}
