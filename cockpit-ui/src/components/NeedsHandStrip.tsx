import type { Attention, SettlementCard } from '../lib/api'
import type { ScreenId } from '../lib/screens'

/* The Needs-Your-Hand strip: pending items ARE lamps. It merges TWO kinds of
   source, deliberately kept separate:

   1. The forward-books settlement cards (`cards`, derived from the already-fetched
      forward-books payload — plan scope decision 8, no extra endpoint). These are
      the "…"-while-unknown source: `cards: null` means the payload hasn't arrived
      (or errored, force-nulled by App), which must render "…", never the empty
      state — an unknown must not read as "nothing needs your hand".

   2. The permanent `/api/attention` poll's client-known facts: queued proposals
      ("· decide"), approved-pending-promotion ("· promote" — an approval MARKS,
      a human promotes; the item clears once the name lands in the experiment
      registry), reflection-due play types, unacked System-Audit breaches,
      pending coach tag-confirms, open reflection/optimizer GitHub PRs (the
      loop's off-app accept/reject gate), and the unread deep analysis
      ("unread" is a client-side localStorage compare done in App and passed as
      a bool). These are ALWAYS-known facts (last-good attention is fine for a
      low-stakes nudge) and never gate the cards' "…".

   EVERY item is a door that lands where it is acted on: proposals + reflection
   → Playbooks; settlements → Forward Books; breaches → System Audit; coach →
   Journal; research PRs → the PR itself (browser); unread analysis → Analyst. */

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
            // Informational, not a hand: reflection resolves ITSELF on the
            // weekly workflow's Sunday run (its output arrives later as a PR
            // item above). The copy must say so — an unactionable "due" chip
            // trains the owner to ignore the strip.
            <button
              key={`r:${pt}`}
              type="button"
              className="needs-hand-item needs-hand-btn"
              title="this play type's forward book re-armed a reflection — the weekly workflow runs it automatically on Sunday (its edge-file PR will appear here for review); nothing needs your hand now. Click to review the playbook."
              onClick={nav('playbooks')}
            >
              {pt} reflection due · auto Sun
            </button>
          )),
          ...((attention.audit_unacked ?? 0) > 0
            ? [
                <button
                  key="audit"
                  type="button"
                  className={
                    attention.audit_worst === 'alert'
                      ? 'needs-hand-item needs-hand-btn needs-hand-alert'
                      : 'needs-hand-item needs-hand-btn'
                  }
                  title="System-Audit findings awaiting your acknowledgment — click to review"
                  onClick={nav('systemaudit')}
                >
                  {attention.audit_unacked} audit{' '}
                  {attention.audit_unacked === 1 ? 'finding' : 'findings'} · ack
                </button>,
              ]
            : []),
          ...((attention.coach_pending ?? 0) > 0
            ? [
                <button
                  key="coach"
                  type="button"
                  className="needs-hand-item needs-hand-btn"
                  title="coach reviews with tag proposals awaiting your confirm — click to open the Journal"
                  onClick={nav('journal')}
                >
                  {attention.coach_pending} coach{' '}
                  {attention.coach_pending === 1 ? 'review' : 'reviews'} · confirm
                </button>,
              ]
            : []),
          ...(attention.research_prs ?? []).map((pr) => (
            <a
              key={`pr:${pr.url}`}
              className="needs-hand-item needs-hand-btn needs-hand-link"
              href={pr.url}
              target="_blank"
              rel="noreferrer"
              title={`${pr.title} — the ${pr.kind} run's output arrives as a GitHub PR; merging (or closing) it IS the accept/reject`}
            >
              {pr.kind} PR · review
            </a>
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
            // The most decision-forcing item in the app must be a DOOR like its
            // siblings — it lands on the Forward wall, where the card carries
            // the stopping rule and the retire procedure.
            <button
              key={c.name}
              type="button"
              className="needs-hand-item needs-hand-btn"
              title="this experiment's stopping rule has fired — click to read the card and decide (a settle/retire PR)"
              onClick={nav('forward')}
            >
              {c.name} ·{' '}
              {c.state === 'settled-awaiting-decision' ? 'settled' : 'futile'} —
              decide
            </button>
          ))}
          {clientItems}
        </>
      )}
    </div>
  )
}
