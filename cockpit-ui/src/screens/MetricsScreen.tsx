import { useState } from 'react'
import { POLL_MS, getScoreboard, usePolling } from '../lib/api'
import type { ScoreboardCard, Window } from '../lib/api'
import { BookMetricCard } from '../components/BookMetricCard'
import { PanelBody } from '../components/PanelBody'
import { Segmented } from '../components/Segmented'
import { Sparkline } from '../components/Sparkline'

/* Screen — Metrics scoreboard (plan Task 8): the ONE sanctioned place a cross-book
   aggregate is allowed to exist (NORTH_STAR #2 — every other surface keeps books
   firewalled). The hero is the combined REAL-MONEY pool (manual equity + live agent,
   both R, deliberately pooled); the grid tiles each book in its honest unit; the
   footer draws the pooled cumulative-R curve.

   HONESTY POSTURE:
   - R-first: expectancy rides through StatChip (the only R renderer) inside each
     BookMetricCard; $ shows only where real dollars exist (manual equity, options,
     live). Robinhood options are $-only and NEVER enter the R pool — a different unit.
   - the 4 book cards render in a FIXED order (manual_equity, robinhood, live, paper)
     so the scoreboard never reshuffles between polls; a book missing from the wire is
     skipped, never faked (the backend always returns all four).
   - the footer curve is OMITTED entirely when the pool has no closes (equity_r null or
     empty) — an absent curve, never a flat fabricated line. */

/** The book tiles in their fixed display order with the parent-owned title/caption
 * (BookMetricCard reads only the shared card fields — the book→copy mapping lives
 * here, never in the tile). */
const BOOK_TILES: {
  book: ScoreboardCard['book']
  title: string
  caption: string
}[] = [
  {
    book: 'manual_equity',
    title: 'Manual equity',
    caption: 'your real equity trades',
  },
  {
    book: 'robinhood',
    title: 'Manual options',
    caption: 'Robinhood premium P&L · no R',
  },
  { book: 'live', title: 'Live agent', caption: 'agent-executed live fills' },
  {
    book: 'paper',
    title: 'Paper',
    caption: 'curated intent book (baseline arm, default variant)',
  },
]

export function MetricsScreen({ wake }: { wake: number }) {
  // The screen owns its window cut; win is the poll paramsKey so a window flip
  // re-params the fetch WITHOUT a full remount (the MissionControl/ForwardScreen
  // idiom), keeping the panel steady across the swap.
  const [win, setWin] = useState<Window>('all')
  const board = usePolling(() => getScoreboard(win), POLL_MS, wake, win)

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          METRICS
          <span className="panel-caption">
            wins &amp; P&amp;L across your trading books · the one sanctioned
            cross-book aggregate (manual + live) · R-first, $ where it exists
          </span>
          <Segmented
            className="metrics-win"
            options={[
              { value: 'all' },
              { value: '90', label: '90d' },
              { value: '180', label: '180d' },
              { value: '365', label: '365d' },
            ]}
            value={win}
            onChange={setWin}
            title="window"
          />
        </div>
        <PanelBody polled={board} noun="scoreboard">
          {(data) => {
            const byBook = new Map(data.cards.map((c) => [c.book, c]))
            return (
              <div className="metrics">
                <div className="metrics-hero">
                  <BookMetricCard
                    card={data.combined}
                    title="Combined real money"
                    caption="manual equity + live agent, pooled deliberately (both real money, both R) · Robinhood options kept separate (different unit)"
                  />
                </div>

                <div className="metrics-grid">
                  {BOOK_TILES.map(({ book, title, caption }) => {
                    const card = byBook.get(book)
                    // The backend always returns all four; guard the lookup so a
                    // shouldn't-happen gap SKIPS the tile rather than crashing.
                    if (card === undefined) return null
                    return (
                      <BookMetricCard
                        key={book}
                        card={card}
                        title={title}
                        caption={caption}
                      />
                    )
                  })}
                </div>

                {data.combined.equity_r !== null &&
                  data.combined.equity_r.length > 0 && (
                    <div className="metrics-curve">
                      <div className="metrics-curve-label">
                        cumulative R · real money
                      </div>
                      <Sparkline points={data.combined.equity_r} />
                    </div>
                  )}
              </div>
            )
          }}
        </PanelBody>
      </section>
    </main>
  )
}
