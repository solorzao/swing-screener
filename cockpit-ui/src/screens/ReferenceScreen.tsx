import { useEffect, useState } from 'react'
import { POLL_MS, getEmails, getExits, getUniverse, usePolling } from '../lib/api'
import type { ExitRow, UniverseRow } from '../lib/api'
import { GlossaryPanel } from '../components/GlossaryPanel'
import { PanelBody } from '../components/PanelBody'
import { Segmented } from '../components/Segmented'

/* Screen 10 — Reference (plan Task 20): the raw log surfaces the retired
   Streamlit pages carried — the screening universe, the digest (email) log, and
   the filterable exit log. This is the Task-21 deletion gate's surface: the exit
   log especially must be here in full before the dashboard can go.

   HONESTY POSTURE:
   - the exit log SHOWS manual_close rows. They are excluded from EMAIL alerts
     (pending_exit_alerts skips them so the hourly job never pages Oliver about a
     close he just performed) — but they are NOT hidden from the ledger. The
     distinction is the honest one: quiet alerts, complete audit trail. A tag
     marks them so the "why no alert" is legible.
   - realized R/$ rides inside the exit MESSAGE verbatim (the writer composed it);
     there is no separate realized column on the wire, so none is fabricated.
   - Book (is_paper) and Account are DIFFERENT axes — the research grid and the
     curated intent book are BOTH is_paper=true, so the two filters compose (AND)
     rather than duplicate. The controls say so.
   - nullable universe cells (market cap, dollar volume, sector) render an em dash,
     never a fabricated 0.

   SEARCH / FILTER idiom: text inputs (universe search, exit reason/account) are
   DEBOUNCED 300ms into the usePolling paramsKey — no fetch-per-keystroke, and the
   sanctioned honest-drop (paramsKey change blanks then refetches) means stale
   rows never render under a new query. The Book segmented control is a click, not
   typing, so it applies immediately. */

const MINUS = '−'

/** A large count → compact $ (…B / …M / …K); null → em dash. Universe caps and
 * dollar volumes only — a display convenience, never a Stat. */
function fmtBig(v: number | null): string {
  if (v === null) return '—'
  const sign = v < 0 ? MINUS : ''
  const a = Math.abs(v)
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(0)}K`
  return `${sign}$${a.toFixed(0)}`
}

/** Debounce a value: the returned value trails `value` by `ms` of quiet — the
 * paramsKey source for a text-input-driven poll (no refetch mid-keystroke). */
function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return debounced
}

/* ---------------- Universe ---------------- */

function UniverseTable({ rows }: { rows: UniverseRow[] }) {
  if (rows.length === 0) {
    return <div className="panel-wait">no universe rows match</div>
  }
  return (
    <div className="ref-table-wrap">
      <table className="ref-table">
        <thead>
          <tr>
            <th>ticker</th>
            <th>name</th>
            <th>exch</th>
            <th>sector</th>
            <th className="ref-num">market cap</th>
            <th className="ref-num">$ volume</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <tr key={u.ticker}>
              <td className="mono ref-tkr">{u.ticker}</td>
              <td className="ref-name">{u.name}</td>
              <td className="ref-exch">{u.exchange}</td>
              <td>{u.sector === null || u.sector === '' ? '—' : u.sector}</td>
              <td className="mono ref-num">{fmtBig(u.market_cap)}</td>
              <td className="mono ref-num">{fmtBig(u.avg_dollar_volume)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function UniversePanel({ wake }: { wake: number }) {
  const [searchRaw, setSearchRaw] = useState('')
  const search = useDebounced(searchRaw, 300)
  const universe = usePolling(
    () => getUniverse(search),
    POLL_MS,
    wake,
    search, // paramsKey: the debounced query — blank-then-refetch on change
  )

  return (
    <section className="panel">
      <div className="panel-head">
        UNIVERSE
        <span className="panel-caption">the screening universe · ticker search</span>
        <span className="spacer" />
        <input
          className="ref-search mono"
          value={searchRaw}
          maxLength={32}
          placeholder="ticker…"
          aria-label="ticker search"
          onChange={(e) => setSearchRaw(e.target.value)}
        />
      </div>
      <PanelBody polled={universe} noun="universe">
        {(data) => (
          <>
            <div className="ref-count">{data.rows.length} rows</div>
            <UniverseTable rows={data.rows} />
          </>
        )}
      </PanelBody>
    </section>
  )
}

/* ---------------- Exit log ---------------- */

type BookFilter = 'all' | 'paper' | 'real'
const BOOKS: BookFilter[] = ['all', 'paper', 'real']

function ExitTable({ rows }: { rows: ExitRow[] }) {
  if (rows.length === 0) {
    return <div className="panel-wait">no exits match these filters</div>
  }
  return (
    <div className="ref-table-wrap">
      <table className="ref-table ref-exits">
        <thead>
          <tr>
            <th>date</th>
            <th>reason</th>
            <th>book</th>
            <th>account</th>
            <th>tier</th>
            <th>detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => {
            const manual = e.reason === 'manual_close'
            return (
              <tr key={e.id} className={manual ? 'ref-exit-manual' : undefined}>
                <td className="mono">{e.date}</td>
                <td>
                  <span className="ref-reason">{e.reason}</span>
                  {manual && (
                    <span
                      className="ref-manual-tag"
                      title="a close you performed yourself — SHOWN in the ledger, but excluded from the hourly email alert (pending_exit_alerts skips manual_close so you're never paged about your own close)"
                    >
                      no alert
                    </span>
                  )}
                </td>
                <td className="ref-book">{e.is_paper ? 'paper' : 'real'}</td>
                <td>{e.account === '' ? '—' : e.account}</td>
                <td>{e.tier === '' ? '—' : e.tier}</td>
                {/* realized R/$ rides inside the message verbatim — no separate
                    column on the wire, so none is fabricated. */}
                <td className="ref-exit-msg">{e.message === '' ? '—' : e.message}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ExitLogPanel({ wake }: { wake: number }) {
  const [reasonRaw, setReasonRaw] = useState('')
  const [accountRaw, setAccountRaw] = useState('')
  const [book, setBook] = useState<BookFilter>('all')
  const reason = useDebounced(reasonRaw, 300)
  const account = useDebounced(accountRaw, 300)

  const filters = {
    reason: reason.trim() === '' ? undefined : reason.trim(),
    book: book === 'all' ? undefined : book,
    account: account.trim() === '' ? undefined : account.trim(),
  }
  const exits = usePolling(
    () => getExits(filters),
    POLL_MS,
    wake,
    // paramsKey composes the three facets — any change blanks then refetches.
    `${filters.reason ?? ''}|${filters.book ?? ''}|${filters.account ?? ''}`,
  )

  return (
    <section className="panel">
      <div className="panel-head">
        EXIT LOG
        <span className="panel-caption">
          the full ledger · manual closes are SHOWN here (they’re only excluded from
          email alerts) · Book and Account are different axes and compose
        </span>
      </div>
      <div className="ref-filters">
        <label className="ref-filter">
          <span className="ref-filter-lab">reason</span>
          <input
            className="ref-filter-in mono"
            value={reasonRaw}
            maxLength={32}
            placeholder="e.g. target, manual_close"
            onChange={(e) => setReasonRaw(e.target.value)}
          />
        </label>
        <label className="ref-filter">
          <span className="ref-filter-lab">account</span>
          <input
            className="ref-filter-in mono"
            value={accountRaw}
            maxLength={16}
            placeholder="e.g. research, live"
            onChange={(e) => setAccountRaw(e.target.value)}
          />
        </label>
        <span className="ref-filter">
          <span className="ref-filter-lab">book</span>
          <Segmented
            title="book (is_paper) — a different axis from account"
            options={BOOKS.map((b) => ({ value: b, label: b }))}
            value={book}
            onChange={setBook}
          />
        </span>
      </div>
      <PanelBody polled={exits} noun="exit log">
        {(data) => (
          <>
            <div className="ref-count">{data.exits.length} exits</div>
            <ExitTable rows={data.exits} />
          </>
        )}
      </PanelBody>
    </section>
  )
}

/* ---------------- Digest (email) log ---------------- */

function EmailPanel({ wake }: { wake: number }) {
  const emails = usePolling(getEmails, POLL_MS, wake)
  return (
    <section className="panel">
      <div className="panel-head">
        DIGEST LOG
        <span className="panel-caption">sent emails, newest first (last 100)</span>
      </div>
      <PanelBody polled={emails} noun="digest log">
        {(data) =>
          data.emails.length === 0 ? (
            <div className="panel-wait">no emails sent yet</div>
          ) : (
            <div className="ref-table-wrap">
              <table className="ref-table">
                <thead>
                  <tr>
                    <th>sent</th>
                    <th>kind</th>
                    <th>subject</th>
                    <th>run date</th>
                  </tr>
                </thead>
                <tbody>
                  {data.emails.map((m) => (
                    <tr key={m.id}>
                      <td className="mono ref-sent">{m.sent_at ?? '—'}</td>
                      <td className="ref-kind">{m.kind}</td>
                      <td className="ref-subject">{m.subject}</td>
                      <td className="mono">{m.run_date ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

export function ReferenceScreen({ wake }: { wake: number }) {
  return (
    <main className="grid-single">
      <GlossaryPanel />
      <UniversePanel wake={wake} />
      <ExitLogPanel wake={wake} />
      <EmailPanel wake={wake} />
    </main>
  )
}
