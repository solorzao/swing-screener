import type { Polled, TickerEvent, TickerFeed, TickerSource } from '../lib/api'

/* Zone E — the event ticker (plan Task 20): the merged reverse-chron activity
   feed (exits · executions · emails · analyst calls · deep-analysis requests),
   the always-visible bottom strip across EVERY screen — the third persistent
   chrome element beside the masthead and the Needs-Your-Hand strip. Its
   /api/ticker poll lives at App level on the permanent roster (like health /
   beats / gate / forward-books / attention); this component only renders.

   Scroll idiom (per the plan's Task-20 guidance — "a simple scrollable strip is
   fine, DON'T use a janky animation"): a single horizontally-scrollable row of
   source-tagged pills, newest on the LEFT (the wire is already newest-first — the
   merge sorts (ts, source, id) descending — so this maps events in order and
   NEVER re-sorts). Left-to-right is the one unambiguous reading of a reverse-chron
   feed; a two-row column-flow (the design's literal "2 rows") scrambles recency,
   so this collapses to one honest row — a disclosed deviation.

   Every field renders WIRE-VERBATIM: source / ticker / headline in the pill,
   detail + the exit facets (account · book · reason · tier) in the hover title.
   Stored-error detail was already whitelist-sanitized server-side
   (_stored_error_detail) — this adds no trust. A fetch error keeps the last-good
   feed on screen (a slightly stale list of PAST events is harmless and expected —
   this is not a safety signal); only a never-yet-fetched strip shows "…". */

const SOURCE_LABEL: Record<TickerSource, string> = {
  exit: 'EXIT',
  execution: 'EXEC',
  email: 'MAIL',
  analyst: 'CALL',
  analysis: 'DEEP',
}

/** The hover title: the headline's supporting detail plus, for exit rows, the
 * exit-log facet axes (Book=is_paper and Account are DIFFERENT axes) — the same
 * facts the Reference screen's exit log filters on, verbatim. */
function eventTitle(ev: TickerEvent): string {
  const parts: string[] = []
  if (ev.detail.trim() !== '') parts.push(ev.detail)
  if (ev.source === 'exit') {
    const facets: string[] = []
    if (ev.account !== undefined) facets.push(`account ${ev.account}`)
    if (ev.is_paper !== undefined) facets.push(ev.is_paper ? 'paper' : 'real')
    if (ev.reason !== undefined) facets.push(`reason ${ev.reason}`)
    if (ev.tier !== undefined && ev.tier !== '') facets.push(`tier ${ev.tier}`)
    if (facets.length > 0) parts.push(facets.join(' · '))
  }
  parts.push(ev.ts)
  return parts.join('\n')
}

function EventPill({ ev }: { ev: TickerEvent }) {
  return (
    <span className={`et-pill et-src-${ev.source}`} title={eventTitle(ev)}>
      <span className="et-tag">{SOURCE_LABEL[ev.source]}</span>
      {ev.ticker !== null && ev.ticker !== '' && (
        <span className="et-ticker mono">{ev.ticker}</span>
      )}
      <span className="et-headline">{ev.headline}</span>
    </span>
  )
}

export function EventTicker({ feed }: { feed: Polled<TickerFeed> }) {
  // Last-good on error (see the header note): keep the events we have, whether or
  // not the newest fetch failed. Only a truly empty state distinguishes "no data
  // yet" (…) from "the feed is genuinely empty" (no events yet).
  const events = feed.data?.events ?? null

  return (
    <div className="et" role="log" aria-label="event ticker">
      <span className="et-label">FEED</span>
      <div className="et-scroll">
        {events === null ? (
          <span className="et-empty">…</span>
        ) : events.length === 0 ? (
          <span className="et-empty">no events yet</span>
        ) : (
          // Index key: the feed has no wire id and is replaced wholesale each
          // poll (no in-place reordering), so positional keys are correct here.
          events.map((ev, i) => <EventPill key={i} ev={ev} />)
        )}
      </div>
    </div>
  )
}
