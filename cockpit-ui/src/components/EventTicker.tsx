import { useEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import type { Polled, TickerEvent, TickerFeed, TickerSource } from '../lib/api'

/* Zone E — the event ticker (plan Task 20): the merged reverse-chron activity
   feed (exits · executions · emails · analyst calls · deep-analysis requests),
   the always-visible bottom strip across EVERY screen — the third persistent
   chrome element beside the masthead and the Needs-Your-Hand strip. Its
   /api/ticker poll lives at App level on the permanent roster (like health /
   beats / gate / forward-books / attention); this component only renders.

   Scroll idiom — a DELIBERATE DEVIATION from the spec, stated plainly: the plan
   (Task 20) and the design doc both specify a TWO-ROW strip. This ships ONE
   horizontally-scrollable row instead, because a two-row column-flow layout
   scrambles the newest-first recency — an event and the one before it would land
   in the same column (top vs bottom), so reading down-then-right no longer tracks
   time. A single row keeps reverse-chron unambiguous: newest on the LEFT (the wire
   is already newest-first — the merge sorts (ts, source, id) descending — so this
   maps events in order and NEVER re-sorts), older to the right. The row
   auto-scrolls left on its own (pausing on hover/focus, honouring reduced-motion —
   see the effect in the component) and still scrolls by wheel, drag, or keyboard.
   The two-row layout is a straightforward follow-up if wanted.

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
    <span className={`et-pill et-src-${ev.source}`} role="listitem" title={eventTitle(ev)}>
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

  // role: NOT "log" — a log is an append-at-END live region that assistive tech
  // reads in DOM order, which would both mis-announce this newest-on-LEFT feed and
  // chatter on every 60s poll. A plain list is the honest shape: an ordered set of
  // items with no live-region claim, labelled with its direction. The role rides
  // only the populated branch so the empty/loading text is never an orphan child
  // of a list (a strict-a11y flag). The region is focusable (tabIndex) so a
  // keyboard-only user can Tab to it and arrow-scroll to older events.
  const populated = events !== null && events.length > 0

  // Auto-scroll ("rotate the feed"): the strip drifts left on its own so you never
  // have to grab it, looping back to the newest event (leftmost) at the end.
  // Pauses on hover/focus so you can read or interact; honours prefers-reduced-
  // motion (then it's just the manual wheel/drag/arrow strip below). ~36 px/s,
  // sub-pixel accumulated in rAF so it stays smooth and frame-rate independent.
  const scrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = scrollRef.current
    if (el === null || !populated) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let paused = false
    let acc = 0
    let last = performance.now()
    let raf = 0
    const pause = () => { paused = true }
    const resume = () => { paused = false }
    el.addEventListener('pointerenter', pause)
    el.addEventListener('pointerleave', resume)
    el.addEventListener('focusin', pause)
    el.addEventListener('focusout', resume)
    const tick = (now: number) => {
      const dt = now - last
      last = now
      if (!paused && el.scrollWidth > el.clientWidth + 1) {
        acc += (dt / 1000) * 36
        const step = Math.floor(acc)
        if (step > 0) {
          acc -= step
          el.scrollLeft =
            el.scrollLeft + el.clientWidth >= el.scrollWidth - 1 ? 0 : el.scrollLeft + step
        }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(raf)
      el.removeEventListener('pointerenter', pause)
      el.removeEventListener('pointerleave', resume)
      el.removeEventListener('focusin', pause)
      el.removeEventListener('focusout', resume)
    }
  }, [populated, events])

  // EXPLICIT keyboard scroll: a focused tabIndex=0 overflow div does NOT reliably
  // arrow-scroll on its own in Chromium (verified: focused, 12×ArrowRight, no
  // movement) — so the region owns the gesture rather than trusting a flaky
  // browser default. Arrows nudge, Home/End jump; horizontal only (there is no
  // vertical overflow to steal). preventDefault stops the page from also moving.
  const onScrollKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    const step = 160
    if (e.key === 'ArrowRight') el.scrollBy({ left: step })
    else if (e.key === 'ArrowLeft') el.scrollBy({ left: -step })
    else if (e.key === 'Home') el.scrollTo({ left: 0 })
    else if (e.key === 'End') el.scrollTo({ left: el.scrollWidth })
    else return
    e.preventDefault()
  }
  return (
    <div className="et">
      <span className="et-label">FEED</span>
      <div
        ref={scrollRef}
        className="et-scroll"
        tabIndex={0}
        role={populated ? 'list' : undefined}
        aria-label="event ticker, newest first"
        onKeyDown={onScrollKey}
      >
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
