import { useState } from 'react'
import {
  ApiError,
  POLL_MS,
  getAuditBreaches,
  getAuditReports,
  postAuditAck,
  usePolling,
} from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type { AuditReport } from '../lib/api'
import { PanelBody } from '../components/PanelBody'

/* SYSTEM AUDIT — the System Behavior Auditor's surface (screen 13, masthead-link).
   An INDEPENDENT, read-only view of the MACHINE's conduct: weekly conduct reports +
   an immediate breach feed. Distinct from the personal JOURNAL by construction — it
   reads only machine telemetry, never Oliver's own trades. Nothing here changes
   config or money; the human acknowledges, the human acts. */

/** Severity → a small colored badge class (alert loudest, info calmest). */
function sevClass(severity: string): string {
  if (severity === 'alert') return 'jr-down'
  if (severity === 'warn') return 'jr-flat'
  return 'jr-up'
}

/** A compact, code-owned findings preview (never the prose) for one audit row. */
function findingsPreview(findings: Record<string, unknown>): string {
  const comp = (findings.compliance ?? {}) as Record<string, unknown>
  const anom = (findings.anomaly ?? {}) as Record<string, unknown>
  const bits: string[] = []
  const caps = comp.cap_breaches
  if (Array.isArray(caps)) bits.push(`${caps.length} cap breach(es)`)
  if (comp.reject_rate != null) bits.push(`reject ${String(comp.reject_rate)}`)
  if (comp.n_disarms != null) bits.push(`${String(comp.n_disarms)} disarm(s)`)
  if (anom.drought_days != null) bits.push(`${String(anom.drought_days)} drought day(s)`)
  if (anom.orphan_exit_events != null) bits.push(`${String(anom.orphan_exit_events)} orphan exit(s)`)
  if (findings.cap_breach != null) bits.push('cap breach')
  if (findings.disarm_day != null) bits.push(`disarm ${String(findings.disarm_day)}`)
  return bits.join(' · ')
}

function AuditRow({ a, onAck }: { a: AuditReport; onAck: (id: number) => void }) {
  return (
    <div className="jr-note">
      <div className="jr-note-meta">
        <span className={`jr-note-kind ${sevClass(a.severity)}`}>{a.severity}</span>
        <span className="mono">
          {a.period_from}
          {a.period_to !== a.period_from ? `…${a.period_to}` : ''}
        </span>
        {a.breach_key !== '' && <span className="jr-note-tag mono">{a.breach_key}</span>}
        <span className="spacer" />
        {a.acknowledged ? (
          <span className="jr-note-src">acknowledged</span>
        ) : (
          <button type="button" className="ltf-btn" onClick={() => onAck(a.id)}>
            ACKNOWLEDGE
          </button>
        )}
      </div>
      <div className="jr-note-text">{a.narrative ?? '(no narrative)'}</div>
      <span className="jr-note-hint mono">{findingsPreview(a.findings)}</span>
    </div>
  )
}

/** The ack rejection → one human line (the NotebookPanel error idiom): the
 * server's safe detail on an ApiError, honest-uncertain when unreachable. A
 * swallowed failure here looked exactly like success (the row just stayed). */
function ackErrorText(err: unknown): string {
  return err instanceof ApiError
    ? err.message
    : 'backend unreachable — the acknowledge may not have landed'
}

function BreachesPanel({ wake }: { wake: number }) {
  const [bump, setBump] = useState(0)
  const [ackError, setAckError] = useState<string | null>(null)
  const breaches = usePolling(() => getAuditBreaches(), POLL_MS, wake + bump)
  const ack = (id: number) => {
    setAckError(null)
    postAuditAck(id).then(
      () => setBump((b) => b + 1),
      (err: unknown) => setAckError(ackErrorText(err)),
    )
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <HelpTerm term="BREACH">BREACHES</HelpTerm>
        <span className="panel-caption">immediate hard breaches (caps, <HelpTerm term="DISARM">disarms</HelpTerm>) — flagged on the next run</span>
      </div>
      {ackError !== null && (
        <div className="gex-err" role="alert">
          {ackError}
        </div>
      )}
      <PanelBody polled={breaches} noun="breaches">
        {(rows: AuditReport[]) =>
          rows.length === 0 ? (
            <div className="panel-wait">no breaches — the machine stayed within its rules</div>
          ) : (
            <div className="jr-notes">
              {rows.map((a) => (
                <AuditRow key={a.id} a={a} onAck={ack} />
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

function ReportsPanel({ wake }: { wake: number }) {
  const [bump, setBump] = useState(0)
  const [ackError, setAckError] = useState<string | null>(null)
  const reports = usePolling(() => getAuditReports(), POLL_MS, wake + bump)
  const ack = (id: number) => {
    setAckError(null)
    postAuditAck(id).then(
      () => setBump((b) => b + 1),
      (err: unknown) => setAckError(ackErrorText(err)),
    )
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <HelpTerm term="weekly conduct">WEEKLY CONDUCT</HelpTerm>
        <span className="panel-caption">the <HelpTerm term="auditor">auditor</HelpTerm>'s weekly sweep — did the agents follow their own rules</span>
      </div>
      {ackError !== null && (
        <div className="gex-err" role="alert">
          {ackError}
        </div>
      )}
      <PanelBody polled={reports} noun="reports">
        {(rows: AuditReport[]) =>
          rows.length === 0 ? (
            <div className="panel-wait">no weekly audit yet</div>
          ) : (
            <div className="jr-notes">
              {rows.map((a) => (
                <AuditRow key={a.id} a={a} onAck={ack} />
              ))}
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

export function SystemAuditScreen({ wake }: { wake: number }) {
  return (
    <main className="grid-single">
      <div className="jr-toolbar">
        <span className="jr-toolbar-lab">AUDIT</span>
        <span className="jr-toolbar-note">
          independent oversight of the machine's conduct — read-only; the auditor reports, you act
        </span>
      </div>
      <BreachesPanel wake={wake} />
      <ReportsPanel wake={wake} />
    </main>
  )
}
