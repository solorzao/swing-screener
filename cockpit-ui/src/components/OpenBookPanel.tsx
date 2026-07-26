import type { KeyboardEvent } from 'react'
import { POLL_MS, getOpenBook, usePolling } from '../lib/api'
import type { Facet, OpenBook, OpenBookRow } from '../lib/api'
import { dashOr, fmtClock, fmtPct, fmtR, fmtUsd } from '../lib/fmt'
import { HelpTerm } from './HelpTerm'
import { PanelBody } from './PanelBody'

/* The OPEN BOOK — the currently-RUNNING paper trades, at trade granularity.

   This is the surface the forward wall aggregates away: one row per open
   research-book trade (baseline arm / default variant — the slice reflection
   grades), live-graded against the last close. Facet=gold narrows to the
   would-have-hit-your-inbox rows. Per-row degradation mirrors the Positions
   table: a failed quote nulls that row's live cells (dash + tooltip), never
   the panel. Render is capped (no silent truncation — the caption counts). */

const RENDER_CAP = 300

const price = (v: number | null): string => dashOr(v, (n) => fmtUsd(n))

function Row({ row }: { row: OpenBookRow }) {
  const qTitle =
    row.quote_error !== null ? `quote unavailable (${row.quote_error})` : undefined
  const rTone =
    row.unrealized_r === null ? '' : row.unrealized_r >= 0 ? ' ob-pos' : ' ob-neg'
  return (
    <tr>
      <td className="pos-name mono">{row.ticker}</td>
      <td className="left">{row.play_type}</td>
      <td className="left">{row.strength ?? '—'}</td>
      <td className="left">{row.conviction_tier ?? '—'}</td>
      <td className="pos-num">{row.opened_date}</td>
      <td className="pos-num">{row.age_days}d</td>
      <td className="pos-num">{price(row.entry)}</td>
      <td className="pos-num">{price(row.stop)}</td>
      <td className="pos-num">{price(row.target)}</td>
      <td className="pos-num" title={qTitle}>
        {price(row.last_close)}
      </td>
      <td className={`pos-num${rTone}`} title={qTitle}>
        {dashOr(row.unrealized_r, fmtR)}
      </td>
      <td className="pos-num" title={qTitle}>
        {dashOr(row.unrealized_pct, fmtPct)}
      </td>
      <td className="pos-num" title={qTitle}>
        {dashOr(row.to_stop_r, fmtR)}
      </td>
      <td className="pos-num" title={qTitle}>
        {dashOr(row.to_target_r, fmtR)}
      </td>
    </tr>
  )
}

/** EXPLICIT keyboard scroll (the EventTicker convention): a focused tabIndex=0
 * overflow div does NOT reliably arrow-scroll on its own in Chromium, so the
 * region owns the gesture. Arrows nudge, PageUp/Down step a viewport, Home/End
 * jump; vertical only. preventDefault stops the page from also moving. */
function onScrollKey(e: KeyboardEvent<HTMLDivElement>) {
  const el = e.currentTarget
  const step = 48
  if (e.key === 'ArrowDown') el.scrollBy({ top: step })
  else if (e.key === 'ArrowUp') el.scrollBy({ top: -step })
  else if (e.key === 'PageDown') el.scrollBy({ top: el.clientHeight })
  else if (e.key === 'PageUp') el.scrollBy({ top: -el.clientHeight })
  else if (e.key === 'Home') el.scrollTo({ top: 0 })
  else if (e.key === 'End') el.scrollTo({ top: el.scrollHeight })
  else return
  e.preventDefault()
}

export function OpenBookPanel({ facet, wake }: { facet: Facet; wake: number }) {
  const book = usePolling(() => getOpenBook(facet), POLL_MS, wake, facet)

  return (
    <section className="panel">
      <div className="panel-head">
        OPEN BOOK
        <span className="panel-caption">
          the running <HelpTerm term="paper book">paper trades</HelpTerm> behind the
          wall below · prices as of{' '}
          <HelpTerm term="last close">last close</HelpTerm>
          {book.data !== null &&
            book.data.as_of !== null &&
            ` · quotes ${fmtClock(book.data.as_of)}`}
        </span>
      </div>
      <PanelBody polled={book} noun="open book">
        {(data: OpenBook) => (
          <>
            <div className="ob-caption mono">
              {data.book_label} · {data.count} open
              {data.count > RENDER_CAP && ` · showing newest ${RENDER_CAP}`}
            </div>
            {data.rows.length === 0 ? (
              <div className="panel-wait">
                no open trades on this facet — the book fills as the evening screen
                triggers entries
              </div>
            ) : (
              <div
                className="ob-scroll"
                tabIndex={0}
                aria-label="open book table, scrolls vertically"
                onKeyDown={onScrollKey}
              >
                <table className="pos-table">
                  <thead>
                    <tr>
                      <th className="left">ticker</th>
                      <th className="left">play</th>
                      <th className="left">strength</th>
                      <th className="left">
                        <HelpTerm term="conviction tier">tier</HelpTerm>
                      </th>
                      <th>opened</th>
                      <th>age</th>
                      <th>entry</th>
                      <th>stop</th>
                      <th>target</th>
                      <th>last close</th>
                      <th>
                        <HelpTerm term="R-multiple">uR</HelpTerm>
                      </th>
                      <th>u%</th>
                      <th>→stop</th>
                      <th>→target</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.slice(0, RENDER_CAP).map((row) => (
                      <Row key={row.id} row={row} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </PanelBody>
    </section>
  )
}
