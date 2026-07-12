import { useState } from 'react'
import {
  ApiError,
  POLL_MS,
  getJournalBreakdowns,
  getJournalCalendar,
  getJournalCurve,
  getJournalDiscipline,
  getJournalExcursions,
  getJournalMistakes,
  getJournalNotes,
  getJournalRecords,
  getCoachReviews,
  getWeaknesses,
  postConfirmTag,
  postJournalNote,
  usePolling,
} from '../lib/api'
import type {
  BreakdownBy,
  CalendarCell,
  CoachBook,
  CoachReview,
  JournalBook,
  JournalCalendar,
  JournalCurve,
  JournalDiscipline,
  JournalExcursions,
  MistakeRow,
  NoteKind,
  TradeRecordRow,
  WeaknessesProfile,
} from '../lib/api'
import { dashOr, fmtR } from '../lib/fmt'
import { PanelBody } from '../components/PanelBody'
import { Segmented } from '../components/Segmented'
import { Sparkline } from '../components/Sparkline'
import { StatChip } from '../components/StatChip'

/* Screen 11 — Journal (Journal v1): the review surface over one book's realized
   swing book. A BOOK selector (research | paper | live) drives every panel here;
   each book-scoped poll passes `book` as usePolling's paramsKey, so a book flip
   blanks the panel then refetches (the sanctioned honest-drop — App.tsx's facet
   poll does the same), never rendering one book's numbers under another's label.

   HONESTY POSTURE (mirrors the router):
   - every STATISTIC (breakdown buckets, the per-mistake expectancy) rides as a
     full Stat through StatChip — the app's only numeric renderer;
   - descriptive floats that are NOT Stats (excursion means/medians, discipline
     metrics, calendar/curve R) render as plain labeled cells, and a null
     (unmeasured / non-finite) renders an em dash, never a fabricated 0;
   - `cost_level` is the BOOK's vintage stamp, shown once per panel, never per cell;
   - the notebook is DAY-scoped (not book-scoped): its poll ignores the book. */

const MINUS = '−'

const BOOK_OPTIONS: { value: JournalBook; label: string; title: string }[] = [
  { value: 'manual_equity', label: 'manual equity', title: 'your real equity trades (the Coach view)' },
  { value: 'robinhood', label: 'robinhood', title: 'your real options book ($ premium, never R)' },
  { value: 'research', label: 'research', title: 'the wide would-surface grid (machine)' },
  { value: 'paper', label: 'paper', title: 'the curated intent (paper) book (machine)' },
  { value: 'live', label: 'live', title: 'the live-money account (machine)' },
]

/** The two PERSONAL books get the Coach view; the three machine books get the v1
 * stat panels (which are R-native over the shadow book and don't apply here). */
const PERSONAL_BOOKS: ReadonlySet<string> = new Set(['manual_equity', 'robinhood'])
function isPersonalBook(book: JournalBook): book is CoachBook {
  return PERSONAL_BOOKS.has(book)
}

/** Format a review's result with its unit ("2R" / "$42"), em dash when null. */
function fmtResult(result: number | null | undefined, unit: string | undefined): string {
  if (result === null || result === undefined) return '—'
  return unit === '$' ? `$${result}` : `${result}R`
}

/** The book's slippage vintage as a caption stamp — null = mixed / unstamped. */
function costCaption(costLevel: string | null): string {
  return costLevel === null ? 'cost level not stamped (mixed / legacy)' : `net @${costLevel}`
}

/* ================= 1 · P&L CALENDAR ================= */

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** R-sign class for a colored cell/value: positive → up, negative → down, flat. */
function signClass(r: number | null): string {
  if (r === null || r === 0) return 'jr-flat'
  return r > 0 ? 'jr-up' : 'jr-down'
}

/** Days in a calendar month, and the Monday-first column (0..6) its 1st sits in. */
function monthShape(year: number, month1: number): { days: number; lead: number } {
  const first = new Date(year, month1 - 1, 1)
  const lead = (first.getDay() + 6) % 7 // getDay() 0=Sun → Monday-first index
  const days = new Date(year, month1, 0).getDate()
  return { days, lead }
}

function CalendarGrid({ month, days }: { month: string; days: Record<string, CalendarCell> }) {
  const [yearStr, monthStr] = month.split('-')
  const year = Number(yearStr)
  const month1 = Number(monthStr)
  const { days: nDays, lead } = monthShape(year, month1)

  const cells: (number | null)[] = []
  for (let i = 0; i < lead; i++) cells.push(null)
  for (let d = 1; d <= nDays; d++) cells.push(d)

  return (
    <div className="jr-cal">
      <div className="jr-cal-head">
        {WEEKDAYS.map((w) => (
          <div key={w} className="jr-cal-dow">
            {w}
          </div>
        ))}
      </div>
      <div className="jr-cal-grid">
        {cells.map((d, i) => {
          if (d === null) return <div key={`b${i}`} className="jr-cal-cell jr-cal-empty" />
          const iso = `${monthStr === undefined ? '' : month}-${String(d).padStart(2, '0')}`
          const cell = days[iso]
          if (cell === undefined) {
            return (
              <div key={iso} className="jr-cal-cell">
                <span className="jr-cal-day">{d}</span>
              </div>
            )
          }
          return (
            <div key={iso} className={`jr-cal-cell jr-cal-live ${signClass(cell.r)}`}>
              <span className="jr-cal-day">{d}</span>
              <span className="jr-cal-r mono">{dashOr(cell.r, fmtR)}</span>
              <span className="jr-cal-n">
                {cell.n} {cell.n === 1 ? 'trade' : 'trades'}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function CalendarPanel({ book, wake }: { book: JournalBook; wake: number }) {
  // No `month` param: the wire returns EVERY closed day + the whole-cohort month
  // roll-up in one read, so month navigation is a client-side filter (no extra
  // poll). paramsKey=book → blank-then-refetch on a book flip.
  const cal = usePolling(() => getJournalCalendar(book), POLL_MS, wake, book)
  const [selected, setSelected] = useState<string | null>(null)

  const render = (data: JournalCalendar) => {
    const months = Object.keys(data.months).sort()
    if (months.length === 0) {
      return <div className="panel-wait">no closed trades in this book yet</div>
    }
    // A stale selection (e.g. left over from another book) falls back to latest.
    const effective =
      selected !== null && data.months[selected] !== undefined
        ? selected
        : months[months.length - 1]
    const idx = months.indexOf(effective)
    const roll = data.months[effective]

    return (
      <div className="jr-cal-wrap">
        <div className="jr-cal-nav">
          <button
            type="button"
            className="jr-nav-btn"
            disabled={idx <= 0}
            onClick={() => setSelected(months[idx - 1])}
            aria-label="previous month"
          >
            ‹
          </button>
          <div className="jr-cal-nav-mid">
            <span className="jr-cal-month mono">{effective}</span>
            <span className={`jr-cal-roll mono ${signClass(roll.r)}`}>
              {dashOr(roll.r, fmtR)} · {roll.n} {roll.n === 1 ? 'trade' : 'trades'}
            </span>
          </div>
          <button
            type="button"
            className="jr-nav-btn"
            disabled={idx >= months.length - 1}
            onClick={() => setSelected(months[idx + 1])}
            aria-label="next month"
          >
            ›
          </button>
        </div>
        <CalendarGrid month={effective} days={data.days} />
      </div>
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        P&amp;L CALENDAR
        <span className="panel-caption">
          realized R by exit date · {book} book ·{' '}
          {cal.data !== null ? costCaption(cal.data.cost_level) : 'net'}
        </span>
      </div>
      <PanelBody polled={cal} noun="calendar">
        {render}
      </PanelBody>
    </section>
  )
}

/* ================= 2 · EQUITY CURVE + DRAWDOWN ================= */

/** Drop the wire's non-finite→null holes so Sparkline sees clean [iso, R] pairs. */
function clean(points: [string, number | null][]): [string, number][] {
  return points.filter((p): p is [string, number] => p[1] !== null)
}

function CurvePanel({ book, wake }: { book: JournalBook; wake: number }) {
  const curve = usePolling(() => getJournalCurve(book), POLL_MS, wake, book)

  const render = (data: JournalCurve) => {
    const equity = clean(data.curve)
    // Underwater view: negate the (always ≥0) depth so the line hangs BELOW the
    // zero reference — 0 sits at a fresh equity high, dips read as underwater.
    const underwater = clean(data.drawdown).map(
      ([d, v]) => [d, -v] as [string, number],
    )
    if (equity.length === 0) {
      return <div className="panel-wait">no closed trades in this book yet</div>
    }
    const endR = equity[equity.length - 1][1]
    const mdd = data.max_drawdown

    return (
      <div className="jr-curve">
        <div className="jr-curve-block">
          <div className="jr-curve-cap">
            <span className="jr-curve-title">cumulative R</span>
            <span className={`jr-curve-val mono ${signClass(endR)}`}>{fmtR(endR)}</span>
          </div>
          <Sparkline points={equity} width={520} height={72} />
        </div>
        <div className="jr-curve-block">
          <div className="jr-curve-cap">
            <span className="jr-curve-title">underwater · R below peak</span>
            <span className="jr-curve-val mono jr-down">
              {mdd === null || mdd === 0 ? '0.00R' : `${MINUS}${mdd.toFixed(2)}R`}
              <span className="jr-curve-sub"> max drawdown</span>
            </span>
          </div>
          {underwater.length > 0 ? (
            <Sparkline points={underwater} width={520} height={48} />
          ) : (
            <div className="panel-wait">no drawdown to plot</div>
          )}
        </div>
      </div>
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        EQUITY CURVE + DRAWDOWN
        <span className="panel-caption">realized cumulative R · {book} book</span>
      </div>
      <PanelBody polled={curve} noun="equity curve">
        {render}
      </PanelBody>
    </section>
  )
}

/* ================= 3 · BREAKDOWNS ================= */

const BY_OPTIONS: { value: BreakdownBy; label: string; title: string }[] = [
  { value: 'dow', label: 'day of week', title: 'grouped by the weekday of the exit' },
  { value: 'hold', label: 'hold time', title: 'grouped by hold bars' },
  { value: 'symbol', label: 'symbol', title: 'per ticker that traded' },
]

function BreakdownsPanel({ book, wake }: { book: JournalBook; wake: number }) {
  const [by, setBy] = useState<BreakdownBy>('dow')
  const breakdowns = usePolling(
    () => getJournalBreakdowns(book, by),
    POLL_MS,
    wake,
    `${book}|${by}`, // any book/axis flip blanks then refetches
  )

  return (
    <section className="panel">
      <div className="panel-head">
        BREAKDOWNS
        <span className="panel-caption">every bucket a full stat · {book} book</span>
        <span className="spacer" />
        <Segmented
          className="jr-by-tabs"
          title="breakdown axis"
          options={BY_OPTIONS}
          value={by}
          onChange={setBy}
        />
      </div>
      <PanelBody polled={breakdowns} noun="breakdowns">
        {(data) => {
          const rows = Object.entries(data.buckets)
          if (rows.length === 0) {
            return <div className="panel-wait">no trades to break down</div>
          }
          return (
            <div className="jr-buckets">
              {rows.map(([label, stat]) => (
                <div key={label} className="jr-bucket-row">
                  <span className="jr-bucket-lab mono">{label}</span>
                  <StatChip stat={stat} />
                </div>
              ))}
            </div>
          )
        }}
      </PanelBody>
    </section>
  )
}

/* ================= 4 · EXCURSIONS + DISCIPLINE ================= */

/** One labeled metric cell (label above value) — the plain, non-Stat treatment
 * excursion means and discipline metrics ride on (no CI machinery exists). */
function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="jr-metric">
      <span className="jr-metric-lab">{label}</span>
      <span className={`jr-metric-val mono ${tone ?? ''}`}>{value}</span>
    </div>
  )
}

/** A raw R value → signed R string, em dash for null. */
const rOr = (v: number | null): string => dashOr(v, fmtR)
/** A 0..1 rate → whole-percent (unsigned — a rate is not a gain/loss), dash for null. */
const rateOr = (v: number | null): string =>
  v === null ? '—' : `${Math.round(v * 100)}%`

function ExcursionsPanel({ book, wake }: { book: JournalBook; wake: number }) {
  const exc = usePolling(() => getJournalExcursions(book), POLL_MS, wake, book)
  return (
    <section className="panel">
      <div className="panel-head">
        EXCURSIONS
        <span className="panel-caption">MAE / MFE in R · means &amp; medians · {book} book</span>
      </div>
      <PanelBody polled={exc} noun="excursions">
        {(data: JournalExcursions) =>
          data.n === 0 ? (
            <div className="panel-wait">no instrumented trades to measure</div>
          ) : (
            <div className="jr-metrics">
              <Metric label="avg MAE" value={rOr(data.avg_mae_r)} tone="jr-down" />
              <Metric label="avg MFE" value={rOr(data.avg_mfe_r)} tone="jr-up" />
              <Metric label="median MAE" value={rOr(data.median_mae_r)} tone="jr-down" />
              <Metric label="median MFE" value={rOr(data.median_mfe_r)} tone="jr-up" />
              <Metric label="n measured" value={String(data.n)} />
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

function DisciplinePanel({ book, wake }: { book: JournalBook; wake: number }) {
  const disc = usePolling(() => getJournalDiscipline(book), POLL_MS, wake, book)
  return (
    <section className="panel">
      <div className="panel-head">
        DISCIPLINE
        <span className="panel-caption">execution habits over existing columns · {book} book</span>
      </div>
      <PanelBody polled={disc} noun="discipline">
        {(data: JournalDiscipline) =>
          data.n_closed === 0 ? (
            <div className="panel-wait">no closed trades in this book yet</div>
          ) : (
            <div className="jr-metrics">
              <Metric label="giveback (R left)" value={rOr(data.giveback_r)} tone="jr-down" />
              <Metric label="stop-honored rate" value={rateOr(data.stop_honored_rate)} />
              <Metric label="MAE before win" value={rOr(data.avg_mae_before_win)} tone="jr-down" />
              <Metric label="closed" value={String(data.n_closed)} />
              <Metric label="with excursion" value={String(data.n_with_excursion)} />
              <Metric label="wins" value={String(data.n_wins)} />
              <Metric
                label="stopped"
                value={`${data.n_stopped} / ${data.n_with_exit_reason}`}
              />
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/* ================= 5 · MISTAKES ================= */

function MistakesPanel({ book, wake }: { book: JournalBook; wake: number }) {
  const mistakes = usePolling(() => getJournalMistakes(book), POLL_MS, wake, book)
  return (
    <section className="panel">
      <div className="panel-head">
        MISTAKES
        <span className="panel-caption">
          realized cost per mistake, worst first · total R is a plain KPI, the
          per-trade expectancy carries its CI · {book} book
        </span>
      </div>
      <PanelBody polled={mistakes} noun="mistakes">
        {(rows: MistakeRow[]) =>
          rows.length === 0 ? (
            <div className="panel-wait">no tagged mistakes in this book</div>
          ) : (
            <div className="jr-mistakes">
              {rows.map((m) => (
                <div key={m.mistake} className="jr-mistake-row">
                  <div className="jr-mistake-head">
                    <span className="jr-mistake-name">{m.mistake}</span>
                    <span className="jr-mistake-cost mono">
                      total <span className={signClass(m.total_r)}>{rOr(m.total_r)}</span> over{' '}
                      {m.n} {m.n === 1 ? 'trade' : 'trades'}
                    </span>
                  </div>
                  <StatChip stat={m.stat} label="per-trade" />
                </div>
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/* ================= 6 · NOTEBOOK ================= */

const NOTE_KINDS: NoteKind[] = ['premarket', 'postmarket', 'adhoc']

/** Today in the LOCAL calendar as YYYY-MM-DD (the note day the form defaults to). */
function todayIso(): string {
  const d = new Date()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function NotebookPanel({ wake }: { wake: number }) {
  const [day, setDay] = useState<string>(todayIso)
  const [localBump, setLocalBump] = useState(0)
  // Notes are DAY-scoped (no book) — paramsKey is the day; a local bump rides the
  // wake param for an immediate refetch after a successful write.
  const notes = usePolling(() => getJournalNotes(day), POLL_MS, wake + localBump, day)

  const [kind, setKind] = useState<NoteKind>('premarket')
  const [moduleTxt, setModuleTxt] = useState('')
  const [body, setBody] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const submit = () => {
    if (body.trim() === '') return
    setSubmitting(true)
    setFormError(null)
    postJournalNote({
      day,
      kind,
      body,
      module: moduleTxt.trim() === '' ? undefined : moduleTxt.trim(),
    }).then(
      () => {
        setSubmitting(false)
        setBody('')
        setLocalBump((b) => b + 1) // immediate refetch of the day's notes
      },
      (err: unknown) => {
        setSubmitting(false)
        setFormError(
          err instanceof ApiError
            ? err.message
            : 'backend unreachable — the note may or may not have been saved',
        )
      },
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        NOTEBOOK
        <span className="panel-caption">
          day-keyed premarket / postmarket / adhoc notes — your own, stamped server-side
        </span>
        <span className="spacer" />
        <label className="jr-day">
          <span className="jr-day-lab">day</span>
          <input
            className="jr-day-in mono"
            type="date"
            value={day}
            aria-label="notebook day"
            onChange={(e) => setDay(e.target.value === '' ? todayIso() : e.target.value)}
          />
        </label>
      </div>

      <div className="jr-note-form">
        <Segmented
          title="note kind"
          options={NOTE_KINDS.map((k) => ({ value: k, label: k }))}
          value={kind}
          onChange={setKind}
        />
        <input
          className="jr-note-module mono"
          value={moduleTxt}
          maxLength={16}
          placeholder="module (optional)"
          aria-label="note module"
          onChange={(e) => setModuleTxt(e.target.value)}
        />
        <textarea
          className="jr-note-body"
          value={body}
          maxLength={10000}
          rows={3}
          placeholder={`what happened on ${day}…`}
          aria-label="note body"
          onChange={(e) => setBody(e.target.value)}
        />
        {formError !== null && (
          <div className="ltf-err" role="alert">
            {formError}
          </div>
        )}
        <div className="jr-note-actions">
          <button
            type="button"
            className="ltf-btn ltf-submit"
            disabled={submitting || body.trim() === ''}
            onClick={submit}
          >
            {submitting ? 'saving…' : 'ADD NOTE'}
          </button>
          <span className="jr-note-hint">source is stamped “human” · saved to {day}</span>
        </div>
      </div>

      <PanelBody polled={notes} noun="notes">
        {(rows) =>
          rows.length === 0 ? (
            <div className="panel-wait">no notes for {day}</div>
          ) : (
            <div className="jr-notes">
              {rows.map((note) => (
                <div key={note.id} className="jr-note">
                  <div className="jr-note-meta">
                    <span className={`jr-note-kind jr-note-${note.kind}`}>{note.kind}</span>
                    {note.module !== null && note.module !== '' && (
                      <span className="jr-note-tag mono">{note.module}</span>
                    )}
                    <span className="jr-note-src">{note.source}</span>
                    <span className="jr-note-when mono">{note.created_at}</span>
                  </div>
                  <div className="jr-note-text">{note.body}</div>
                </div>
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/* ================= 7 · RECORDS ================= */

function RecordsPanel({ book, wake }: { book: JournalBook; wake: number }) {
  const records = usePolling(() => getJournalRecords(book), POLL_MS, wake, book)
  return (
    <section className="panel">
      <div className="panel-head">
        RECORDS
        <span className="panel-caption">
          every trade in the book with its tags &amp; theses · display only · {book} book
        </span>
      </div>
      <PanelBody polled={records} noun="records">
        {(rows: TradeRecordRow[]) =>
          rows.length === 0 ? (
            <div className="panel-wait">no trades in this book yet</div>
          ) : (
            <div className="ref-table-wrap">
              <table className="ref-table jr-records">
                <thead>
                  <tr>
                    <th>id</th>
                    <th>symbol</th>
                    <th>dir</th>
                    <th>opened</th>
                    <th>closed</th>
                    <th className="ref-num">R</th>
                    <th>tags</th>
                    <th>theses</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.trade_id}>
                      <td className="mono">#{r.trade_id}</td>
                      <td className="mono ref-tkr">{r.symbol}</td>
                      <td>{r.direction}</td>
                      <td className="mono">{r.opened ?? '—'}</td>
                      <td className="mono">{r.closed ?? '—'}</td>
                      <td className={`mono ref-num ${signClass(r.r)}`}>{rOr(r.r)}</td>
                      <td>
                        {r.tags.length === 0 ? (
                          '—'
                        ) : (
                          <span className="jr-rec-chips">
                            {r.tags.map((t, i) => (
                              <span
                                key={i}
                                className="jr-rec-chip"
                                title={`${t.kind} · ${t.source}`}
                              >
                                {t.name}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td>
                        {r.theses.length === 0 ? (
                          '—'
                        ) : (
                          <span className="jr-rec-chips">
                            {r.theses.map((h, i) => (
                              <span key={i} className="jr-rec-chip" title={h.body}>
                                {h.event_kind} · {h.source}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
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

/* ================= screen ================= */

/* ================= COACH REVIEWS (Journal v2) ================= */

/** Per-trade coaching over a personal book. Facts are code-owned (rendered here);
 * `narrative`/`human_edit` are advisory prose. Parked tag proposals confirm into the
 * overlay (source=analyst) on click — the confirm gate. */
function CoachReviewsPanel({ book, wake }: { book: CoachBook; wake: number }) {
  const [bump, setBump] = useState(0)
  const reviews = usePolling(() => getCoachReviews(book), POLL_MS, wake + bump, book)
  const confirm = (id: number, name: string, kind: string) => {
    postConfirmTag(id, { name, kind }).then(
      () => setBump((b) => b + 1),
      () => {},
    )
  }
  return (
    <section className="panel">
      <div className="panel-head">
        COACH REVIEWS
        <span className="panel-caption">
          per-trade coaching · {book} — numbers owned by code, prose advisory
        </span>
      </div>
      <PanelBody polled={reviews} noun="reviews">
        {(rows: CoachReview[]) =>
          rows.length === 0 ? (
            <div className="panel-wait">no reviews yet — they appear as you close manual trades</div>
          ) : (
            <div className="jr-notes">
              {rows.map((r) => (
                <div key={r.id} className="jr-note">
                  <div className="jr-note-meta">
                    <span className="jr-note-kind">{String(r.facts.outcome ?? '—')}</span>
                    <span className="mono">{fmtResult(r.facts.result, r.facts.unit)}</span>
                    {r.facts.moved_stop === true && (
                      <span className="jr-note-tag">moved stop</span>
                    )}
                    {r.facts.emotional_state != null && (
                      <span className="jr-note-tag">{r.facts.emotional_state}</span>
                    )}
                    <span className="jr-note-when mono">{r.generated_at ?? '—'}</span>
                  </div>
                  <div className="jr-note-text">
                    {r.human_edit ?? r.narrative ?? '(draft pending…)'}
                  </div>
                  {(r.facts.tag_proposals ?? []).length > 0 && (
                    <div className="jr-note-actions">
                      <span className="jr-note-hint">confirm a tag:</span>
                      {(r.facts.tag_proposals ?? []).map((p) => (
                        <button
                          key={p.name}
                          type="button"
                          className="ltf-btn"
                          title={p.reason}
                          onClick={() => confirm(r.id, p.name, p.kind)}
                        >
                          {p.name} ({p.kind})
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/** The current Weaknesses Profile — a staleness-stamped distillation, thin-data honest. */
function WeaknessesPanel({ wake }: { wake: number }) {
  const prof = usePolling(() => getWeaknesses(), POLL_MS, wake)
  return (
    <section className="panel">
      <div className="panel-head">
        WEAKNESSES
        <span className="panel-caption">standing patterns across your reviews — a living distillation</span>
      </div>
      <PanelBody polled={prof} noun="profile">
        {(p: WeaknessesProfile) => (
          <div className="jr-weak">
            {p.thin_data && (
              <div className="panel-wait">
                thin data ({p.n_reviews} reviews) — read as a hint, not a verdict
              </div>
            )}
            {p.items.length === 0 ? (
              <div className="panel-wait">no recurring weakness yet</div>
            ) : (
              <ul className="jr-weak-list">
                {p.items.map((it) => (
                  <li key={it.weakness}>
                    {it.weakness}
                    {it.count != null && it.count > 0 ? ` ×${it.count}` : ''}
                  </li>
                ))}
              </ul>
            )}
            {p.generated_at != null && (
              <span className="jr-note-hint">as of {p.generated_at}</span>
            )}
          </div>
        )}
      </PanelBody>
    </section>
  )
}

export function JournalScreen({ wake }: { wake: number }) {
  const [book, setBook] = useState<JournalBook>('manual_equity')
  const personal = isPersonalBook(book)

  return (
    <main className="grid-single">
      <div className="jr-toolbar">
        <span className="jr-toolbar-lab">BOOK</span>
        <Segmented
          title="journal book (account)"
          options={BOOK_OPTIONS}
          value={book}
          onChange={setBook}
        />
        <span className="jr-toolbar-note">
          {personal
            ? 'your real trades — the Coach view (never pooled with the machine books)'
            : 'R-native · per-book firewall — one account, never pooled'}
        </span>
      </div>

      {personal ? (
        <>
          <CoachReviewsPanel book={book} wake={wake} />
          <WeaknessesPanel wake={wake} />
          <RecordsPanel book={book} wake={wake} />
        </>
      ) : (
        <>
          <CalendarPanel book={book} wake={wake} />
          <CurvePanel book={book} wake={wake} />
          <BreakdownsPanel book={book} wake={wake} />
          <div className="jr-two">
            <ExcursionsPanel book={book} wake={wake} />
            <DisciplinePanel book={book} wake={wake} />
          </div>
          <MistakesPanel book={book} wake={wake} />
          <NotebookPanel wake={wake} />
          <RecordsPanel book={book} wake={wake} />
        </>
      )}
    </main>
  )
}
