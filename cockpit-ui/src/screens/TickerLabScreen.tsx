import { useCallback, useState } from 'react'
import {
  ApiError,
  POLL_MS,
  getLabAnalyses,
  getLabBars,
  postLabAnalysis,
  usePolling,
} from '../lib/api'
import type {
  LabAnalysisRow,
  LabBarsPayload,
  LabTimeframe,
} from '../lib/api'
import { fmtClock, fmtUsd } from '../lib/fmt'
import { LabChart } from '../components/LabChart'
import type { LabShow } from '../components/LabChart'
import { Markdown } from '../components/Markdown'
import { PanelBody } from '../components/PanelBody'
import { Segmented } from '../components/Segmented'

/* TICKER LAB — the on-demand per-ticker study for Oliver's own research: any
   ticker, Heiken Ashi candles on 4h/1d/1wk/1mo, toggleable EMA 9/21/50/200,
   MACD, volume, swing-pivot S/R and Fibonacci levels — every number a
   deterministic engine fact rendered verbatim ("as of last close", like every
   price surface). The DEEP ANALYSIS panel sends the full four-timeframe study
   (plus fundamentals/news context) to the Opus analyst at MAX effort; the note
   runs in-process the moment it's requested and renders inline as markdown.

   The study poll is screen-scoped and keyed `${ticker}|${timeframe}` — a
   switch blanks-then-refetches (usePolling's honest drop); the 60s floor
   mostly rides the server's per-day bar cache. The analyses poll follows the
   AnalysisPanel pattern: list poll + SSE wake + a local bump after POST. */

const TF_OPTIONS = [
  { value: '4h', label: '4H', title: '4-hour — carries at most ~60 days (resampled 1h bars)' },
  { value: '1d', label: '1D', title: 'daily' },
  { value: '1wk', label: '1W', title: 'weekly (resampled daily bars)' },
  { value: '1mo', label: '1M', title: 'monthly (resampled daily bars)' },
] as const

const DEFAULT_SHOW: LabShow = {
  e9: true,
  e21: true,
  e50: true,
  e200: true,
  vol: true,
  macd: true,
  sr: true,
  fib: true,
}

function Tog({
  label,
  cls,
  on,
  disabled,
  title,
  onClick,
}: {
  label: string
  cls: string
  on: boolean
  disabled?: boolean
  title?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      className={`lab-tog ${cls}${on && disabled !== true ? ' lab-tog-on' : ''}`}
      aria-pressed={on && disabled !== true}
      disabled={disabled}
      title={title}
      onClick={onClick}
    >
      {label}
    </button>
  )
}

const allNull = (vals: (number | null)[]): boolean => vals.every((v) => v === null)

/** The polled study for one submitted (ticker, timeframe): overlay toggles
 * (doubling as the legend — each chip wears its series color), the meta line,
 * and the chart. Mounted only once a ticker is submitted, so its poll never
 * runs against an empty input. */
function StudyPanel({
  ticker,
  timeframe,
  wake,
}: {
  ticker: string
  timeframe: LabTimeframe
  wake: number
}) {
  const [show, setShow] = useState<LabShow>(DEFAULT_SHOW)
  const toggle = (k: keyof LabShow) => setShow((s) => ({ ...s, [k]: !s[k] }))
  const bars = usePolling<LabBarsPayload>(
    () => getLabBars(ticker, timeframe),
    POLL_MS,
    wake,
    `${ticker}|${timeframe}`,
  )

  return (
    <PanelBody polled={bars} noun="ticker study">
      {(data) => {
        const emaDead = (span: '9' | '21' | '50' | '200') => allNull(data.emas[span])
        const srDead =
          data.levels.support.length === 0 && data.levels.resistance.length === 0
        const emaTitle = (span: string) =>
          `EMA ${span}` +
          (emaDead(span as '9' | '21' | '50' | '200')
            ? ' — insufficient history on this timeframe'
            : ' (computed on real closes)')
        return (
          <>
            <div className="lab-togs">
              <Tog label="EMA 9" cls="lab-tog-e9" on={show.e9} disabled={emaDead('9')}
                title={emaTitle('9')} onClick={() => toggle('e9')} />
              <Tog label="EMA 21" cls="lab-tog-e21" on={show.e21} disabled={emaDead('21')}
                title={emaTitle('21')} onClick={() => toggle('e21')} />
              <Tog label="EMA 50" cls="lab-tog-e50" on={show.e50} disabled={emaDead('50')}
                title={emaTitle('50')} onClick={() => toggle('e50')} />
              <Tog label="EMA 200" cls="lab-tog-e200" on={show.e200} disabled={emaDead('200')}
                title={emaTitle('200')} onClick={() => toggle('e200')} />
              <Tog label="MACD" cls="lab-tog-macd" on={show.macd}
                disabled={allNull(data.macd.hist)}
                title={allNull(data.macd.hist)
                  ? 'MACD 12·26·9 — insufficient history on this timeframe'
                  : 'MACD 12·26·9 sub-pane'}
                onClick={() => toggle('macd')} />
              <Tog label="VOL" cls="lab-tog-vol" on={show.vol}
                title="volume sub-pane" onClick={() => toggle('vol')} />
              <Tog label="S/R" cls="lab-tog-sr" on={show.sr} disabled={srDead}
                title={srDead
                  ? 'no swing pivots detected in this window'
                  : 'clustered swing-pivot support/resistance (real highs/lows, never HA)'}
                onClick={() => toggle('sr')} />
              <Tog label="FIB" cls="lab-tog-fib" on={show.fib} disabled={data.fib === null}
                title={data.fib === null
                  ? 'no drawable swing in this window'
                  : `Fibonacci retracement of the window's ${data.fib.direction}-swing`}
                onClick={() => toggle('fib')} />
            </div>
            <div className="lab-meta mono">
              {data.bar_count} bars · as of {data.as_of} · last close{' '}
              {data.last_close !== null ? data.last_close.toFixed(2) : '—'}
              {timeframe === '4h' && ' · 4h history is capped at ~60 days'}
            </div>
            <LabChart data={data} show={show} />
          </>
        )
      }}
    </PanelBody>
  )
}

/* ---------------- The deep-analysis action + notes ---------------- */

type Phase =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'queued'; id: number }
  | { kind: 'error'; detail: string }

function LabRequestForm({
  ticker,
  onRequested,
}: {
  ticker: string
  onRequested: () => void
}) {
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })

  const submit = () => {
    setPhase({ kind: 'submitting' })
    postLabAnalysis(ticker).then(
      (r) => {
        setPhase({ kind: 'queued', id: r.id })
        onRequested() // local key-bump refetch; SSE wakes other windows
      },
      (err: unknown) => {
        setPhase({
          kind: 'error',
          detail:
            err instanceof ApiError
              ? err.message
              : 'backend unreachable — the request may or may not have queued; ' +
                'if it did, it appears below',
        })
      },
    )
  }

  const busy = phase.kind === 'submitting'
  return (
    <div className="lab-an-form">
      <button
        type="button"
        className="lab-an-submit"
        disabled={busy}
        onClick={submit}
        title="sends the full 4h/1d/1wk/1mo study + fundamentals/news to the Opus analyst at max effort — runs now, in this cockpit process"
      >
        {busy ? 'queueing…' : `request deep analysis · ${ticker}`}
      </button>
      <span className="lab-an-note">
        Opus at max effort · runs in-process now (needs ANTHROPIC_API_KEY) ·
        spend is UNCAPPED; the cost estimate lands on the note
      </span>
      {phase.kind === 'error' && (
        <div className="an-err" role="alert">
          {phase.detail}
        </div>
      )}
      {phase.kind === 'queued' && (
        <div className="an-ok" role="status">
          queued #{phase.id} · {ticker} — the note lands below when the analyst
          finishes
        </div>
      )}
    </div>
  )
}

function LabAnalysisItem({ row }: { row: LabAnalysisRow }) {
  return (
    <div className="lab-an-row">
      <div className="lab-an-head">
        <span className="lab-an-ticker mono">{row.ticker}</span>
        <span className="lab-an-id mono">#{row.id}</span>
        <span className={`an-status an-status-${row.status}`}>
          {row.status}
          {row.stalled && (
            <span
              className="an-stalled"
              title="running past the in-process drain window — there is no requeue pass; the cockpit likely restarted mid-run"
            >
              {' '}
              · stalled, request again
            </span>
          )}
        </span>
        <span className="spacer" />
        <span className="lab-an-times mono">
          {row.model} · {row.reasoning} effort
          {row.est_cost_usd !== null && ` · ~${fmtUsd(row.est_cost_usd)}`}
          {' · req '}
          {fmtClock(row.requested_at)}
          {row.finished_at !== null && ` · done ${fmtClock(row.finished_at)}`}
        </span>
      </div>
      {row.status === 'failed' && (
        <div className="an-error" role="alert">
          {row.error ?? 'analysis failed (no detail recorded)'}
        </div>
      )}
      {(row.status === 'queued' || row.status === 'running') && (
        <div className="an-pending">
          the analyst is thinking at {row.reasoning} effort — a deep note can
          take several minutes
        </div>
      )}
      {row.status === 'done' && (
        <div className="lab-an-report">
          {!row.is_deep && (
            <div className="lab-an-fallback">
              the model call failed — the deterministic study is served verbatim
              below
            </div>
          )}
          <Markdown md={row.report} stripFrontmatter={false} strikeFalsifiedH2={false} />
        </div>
      )}
    </div>
  )
}

function DeepPanel({ ticker, wake }: { ticker: string; wake: number }) {
  const [bump, setBump] = useState(0)
  const onRequested = useCallback(() => setBump((b) => b + 1), [])
  const list = usePolling(
    () => getLabAnalyses(ticker),
    POLL_MS,
    wake + bump,
    ticker,
  )
  return (
    <section className="panel">
      <div className="panel-head">
        DEEP ANALYSIS
        <span className="panel-caption">
          the full 4h/1d/1wk/1mo study + fundamentals/news → the Opus analyst ·
          levels in the note are engine facts
        </span>
      </div>
      <LabRequestForm ticker={ticker} onRequested={onRequested} />
      <PanelBody polled={list} noun="lab analyses">
        {(data) =>
          data.analyses.length === 0 ? (
            <div className="panel-wait">no deep-analysis notes for {ticker} yet</div>
          ) : (
            <div className="lab-an-list">
              {data.analyses.map((row) => (
                <LabAnalysisItem key={row.id} row={row} />
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/* ---------------- The screen ---------------- */

export function TickerLabScreen({ wake }: { wake: number }) {
  const [draft, setDraft] = useState('')
  const [ticker, setTicker] = useState<string | null>(null)
  const [timeframe, setTimeframe] = useState<LabTimeframe>('1d')

  const submit = () => {
    const t = draft.trim().toUpperCase()
    if (t !== '') setTicker(t)
  }

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          TICKER LAB
          <span className="panel-caption">
            on-demand study · Heiken Ashi candles · every level is an engine
            fact, as of last close
          </span>
          <span className="spacer" />
          <Segmented<LabTimeframe>
            options={TF_OPTIONS}
            value={timeframe}
            onChange={setTimeframe}
            title="timeframe"
          />
        </div>
        <form
          className="lab-form"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <input
            className="lab-in mono"
            value={draft}
            maxLength={16}
            placeholder="ticker (e.g. NVDA)"
            aria-label="ticker to study"
            onChange={(e) => setDraft(e.target.value.toUpperCase())}
          />
          <button type="submit" className="lab-study" disabled={draft.trim() === ''}>
            study
          </button>
          {ticker !== null && (
            <span className="lab-current mono" title="the ticker under study">
              {ticker}
            </span>
          )}
        </form>
        {ticker === null ? (
          <div className="panel-wait">
            enter a ticker to study — candles, EMA 9/21/50/200, MACD, volume,
            S/R and Fibonacci levels on 4h/1d/1wk/1mo
          </div>
        ) : (
          <StudyPanel ticker={ticker} timeframe={timeframe} wake={wake} />
        )}
      </section>
      {ticker !== null && <DeepPanel ticker={ticker} wake={wake} />}
    </main>
  )
}
