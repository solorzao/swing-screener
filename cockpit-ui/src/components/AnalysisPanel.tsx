import { useState } from 'react'
import {
  ApiError,
  analysisChartUrl,
  analysisPdfUrl,
  postAnalysis,
} from '../lib/api'
import type { AnalysisList, AnalysisRequestRow, AnalysisStatus, Polled } from '../lib/api'
import { fmtClock } from '../lib/fmt'
import { Markdown } from './Markdown'
import { PanelBody } from './PanelBody'

/* AnalysisPanel — the SIXTH action's surface (plan Task 19): request a deep
   single-ticker analysis, then watch the queue drain and read the report.

   The request is a LIGHT POST, not a hold-to-confirm: it queues a research run
   (the worker spends later, bounded by the run-cost cap) — it moves no venue
   state and closes nothing, so the plan captions it as a plain submit (the holds
   are reserved for DISARM and the proposal decisions, which the plan names
   explicitly). The server owns validation: an empty / non-ASCII / overlong
   ticker comes back 422 and renders inline; a DB outage / guard failure renders
   on the form line; a bare network drop says the request may or may not have
   queued.

   The list is polled by the SCREEN (dies with it) and wakes on the SSE analysis
   watermark; a successful request also local-bumps for an instant refetch. The
   worker-honesty line is rendered VERBATIM from the wire: a local DB has no
   scheduled drain, so its requests wait for a manual ondemand run — the copy
   says so rather than letting a queued row spin as if a cloud worker will pick
   it up. Report assets ride the server byte proxies; a 404 (aged-out blob,
   never-rendered chart) degrades quietly — the thumb hides, the link stays. */

const STATUS_LABEL: Record<AnalysisStatus, string> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  failed: 'failed',
}

function StatusChip({ row }: { row: AnalysisRequestRow }) {
  return (
    <span className={`an-status an-status-${row.status}`}>
      {STATUS_LABEL[row.status]}
      {row.stalled && (
        <span
          className="an-stalled"
          title="running past the worker's requeue window — the next worker pass will requeue it"
        >
          {' '}
          · stalled, will retry
        </span>
      )}
    </span>
  )
}

/** The report body for a DONE request: the summary md (rendered as markdown —
 * both opt-outs OFF so a summary opening with `---` or a "Falsified" heading is
 * never swallowed or struck), the chart thumbs, and the PDF link. Charts and PDF
 * ride the server byte proxies; a 404 is NORMAL (assets age out) — the thumb
 * hides on error, the affordances stay honest about what may be gone. */
function Report({ row }: { row: AnalysisRequestRow }) {
  return (
    <div className="an-report">
      {row.summary.trim() !== '' ? (
        <div className="an-summary">
          <Markdown md={row.summary} stripFrontmatter={false} strikeFalsifiedH2={false} />
        </div>
      ) : (
        <div className="an-summary an-summary-empty">
          no summary text on this report — open the PDF for the full write-up
        </div>
      )}

      {row.chart_count > 0 && (
        <div className="an-charts">
          {Array.from({ length: row.chart_count }, (_, i) => (
            <a
              key={i}
              className="an-chart"
              href={analysisChartUrl(row.id, i)}
              target="_blank"
              rel="noreferrer"
              title="analysis chart — server byte proxy (a 404 here is normal: the asset may have aged out)"
            >
              <img
                className="an-chart-thumb"
                src={analysisChartUrl(row.id, i)}
                alt={`${row.ticker} analysis chart ${i + 1}`}
                loading="lazy"
                onError={(e) => {
                  // 404 = aged-out / never-rendered: hide the broken thumb, keep
                  // the link (clicking still hits the proxy honestly).
                  e.currentTarget.style.display = 'none'
                }}
              />
              <span className="an-chart-cap">chart {i + 1} ↗</span>
            </a>
          ))}
        </div>
      )}

      <div className="an-assets">
        {row.has_pdf ? (
          <a
            className="an-pdf"
            href={analysisPdfUrl(row.id)}
            target="_blank"
            rel="noreferrer"
            title="the full PDF report — server byte proxy (a 404 means the blob aged out)"
          >
            open report PDF ↗
          </a>
        ) : (
          <span className="an-nopdf" title="no PDF on this request yet">
            no PDF
          </span>
        )}
      </div>
    </div>
  )
}

function RequestRow({ row }: { row: AnalysisRequestRow }) {
  return (
    <div className="an-row">
      <div className="an-row-head">
        <span className="an-ticker mono">{row.ticker}</span>
        <span className="an-id mono">#{row.id}</span>
        <StatusChip row={row} />
        <span className="spacer" />
        <span className="an-times mono">
          {/* requested_at is non-null on the wire (the server stamps it at
              creation) — no null branch, unlike started/finished below. */}
          req {fmtClock(row.requested_at)}
          {row.started_at !== null && ` · started ${fmtClock(row.started_at)}`}
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
          {row.status === 'queued'
            ? 'waiting for the worker to claim it'
            : 'the analyst is working — charts and the PDF land when it finishes'}
        </div>
      )}
      {row.status === 'done' && <Report row={row} />}
    </div>
  )
}

/* ---------------- The request form (the action) ---------------- */

type Phase =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'queued'; id: number; ticker: string }
  | { kind: 'error'; detail: string; onTicker: boolean }

function RequestForm({ onRequested }: { onRequested: () => void }) {
  const [ticker, setTicker] = useState('')
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })

  const submit = () => {
    setPhase({ kind: 'submitting' })
    postAnalysis(ticker).then(
      (r) => {
        setPhase({ kind: 'queued', id: r.id, ticker: r.ticker })
        setTicker('')
        onRequested() // local key-bump refetch (scope decision 12); SSE wakes other windows
      },
      (err: unknown) => {
        if (err instanceof ApiError) {
          // 422 field errors ride `fieldErrors`; the ticker is the only field,
          // so any field error is the ticker's — surface it on the input. A
          // hand-raised detail (503 / 409 / 403) rides the form line.
          const onTicker = err.status === 422
          setPhase({ kind: 'error', detail: err.message, onTicker })
        } else {
          setPhase({
            kind: 'error',
            detail:
              'backend unreachable — the request may or may not have queued; ' +
              'if it did, it appears below',
            onTicker: false,
          })
        }
      },
    )
  }

  const busy = phase.kind === 'submitting'
  const tickerErr = phase.kind === 'error' && phase.onTicker ? phase.detail : null
  const formErr = phase.kind === 'error' && !phase.onTicker ? phase.detail : null

  return (
    <div className="an-form">
      <label className="an-lab" htmlFor="an-ticker">
        request deep analysis
      </label>
      <input
        id="an-ticker"
        className="an-in mono"
        value={ticker}
        maxLength={16}
        placeholder="ticker (e.g. AMD)"
        aria-invalid={tickerErr !== null}
        aria-describedby={tickerErr !== null ? 'an-ticker-err' : undefined}
        onChange={(e) => setTicker(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !busy && ticker.trim() !== '') submit()
        }}
      />
      <button
        type="button"
        className="an-submit"
        disabled={busy || ticker.trim() === ''}
        onClick={submit}
        title="queues an on-demand deep-analysis run — the worker runs it later (spend is bounded by the run-cost cap)"
      >
        {busy ? 'queueing…' : 'request'}
      </button>
      <span className="an-form-note">
        queues a research run · the analyst spends later, bounded by the run-cost cap
      </span>

      {tickerErr !== null && (
        <div className="an-err" id="an-ticker-err" role="alert">
          {tickerErr}
        </div>
      )}
      {formErr !== null && (
        <div className="an-err" role="alert">
          {formErr}
        </div>
      )}
      {phase.kind === 'queued' && (
        <div className="an-ok" role="status">
          queued #{phase.id} · {phase.ticker} — it appears in the queue below
        </div>
      )}
    </div>
  )
}

/* ---------------- The panel ---------------- */

export function AnalysisPanel({
  list,
  onRequested,
}: {
  list: Polled<AnalysisList>
  onRequested: () => void
}) {
  return (
    <div className="an">
      <RequestForm onRequested={onRequested} />
      <PanelBody polled={list} noun="analysis requests">
        {(data) => (
          <>
            <div className="an-worker mono">
              {data.worker === 'manual'
                ? 'worker: manual — no scheduled drain on this DB; requests wait for a ' +
                  'manual `python -m swing_screener.notify.ondemand` run'
                : `worker: ${data.worker} — the cloud job drains this queue automatically`}
            </div>
            {data.requests.length === 0 ? (
              <div className="panel-wait">no analysis requests yet</div>
            ) : (
              <div className="an-list">
                {data.requests.map((row) => (
                  <RequestRow key={row.id} row={row} />
                ))}
              </div>
            )}
          </>
        )}
      </PanelBody>
    </div>
  )
}
