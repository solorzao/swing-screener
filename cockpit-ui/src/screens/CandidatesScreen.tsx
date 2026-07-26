import { useEffect, useState } from 'react'
import { POLL_MS, getCohorts, getFunnel, getPicks, usePolling } from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type { CohortRow, Facet, PickRow } from '../lib/api'
import { fmtClock } from '../lib/fmt'
import { resolveCohortStat } from '../lib/picks'
import { FunnelBar } from '../components/FunnelBar'
import { LogTradeForm } from '../components/LogTradeForm'
import { PanelBody } from '../components/PanelBody'
import { PickCard } from '../components/PickCard'
import { Segmented } from '../components/Segmented'

/* Screen 2 — Candidates (plan Task 17): the digest's surfaced picks in digest
   ORDER (daily continuation + reversal) plus the liveness-dropped EXTRAS,
   shown-and-flagged and never counted in the surfaced three (scope decision 7).
   A client-side view toggle defaults to the "digest" set (exactly the email);
   "with extras" reveals the flagged drops. Beneath: the reversal FunnelBar.

   The picks poll lives HERE (per-screen; dies with the screen). Cohorts is
   facet-scoped for the per-card StatChip join. The ONE row action opens a
   prefilled LogTradeForm inline (keyed on the signal id so a new pick reloads);
   a LOG click on Mission Control's Zone D navigates here and hands off its
   signal id via `prefillSignalId`, consumed once into the local logging state. */

type View = 'digest' | 'all'
const VIEWS: View[] = ['digest', 'all']

function PickSection({
  title,
  caption,
  picks,
  cohortRows,
  onLog,
  isExtra = false,
  emptyNote,
}: {
  title: string
  caption?: string
  picks: PickRow[]
  cohortRows: CohortRow[]
  onLog: (signalId: number) => void
  isExtra?: boolean
  emptyNote?: string
}) {
  if (picks.length === 0 && emptyNote === undefined) return null
  return (
    <div className="pk-section">
      <div className="pk-section-head">
        {title}
        {caption !== undefined && <span className="pk-section-cap">{caption}</span>}
      </div>
      {picks.length === 0 ? (
        <div className="pk-section-empty">{emptyNote}</div>
      ) : (
        <div className="pk-grid">
          {picks.map((p) => (
            <PickCard
              key={p.signal_id}
              pick={p}
              isExtra={isExtra}
              cohortStat={resolveCohortStat(cohortRows, p.cohort)}
              onLog={onLog}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function CandidatesScreen({
  facet,
  wake,
  prefillSignalId,
  onPrefillConsumed,
}: {
  facet: Facet
  wake: number
  /** A pending prefill handed off from Mission Control's Zone D LOG action;
   * null when none. Consumed once, then cleared upstream. */
  prefillSignalId: number | null
  onPrefillConsumed: () => void
}) {
  const [localBump, setLocalBump] = useState(0)
  const [view, setView] = useState<View>('digest')
  const [logging, setLogging] = useState<number | null>(null)

  const picks = usePolling(getPicks, POLL_MS, wake + localBump)
  const cohorts = usePolling(() => getCohorts(facet), POLL_MS, wake, facet)
  const funnel = usePolling(getFunnel, POLL_MS, wake)
  const cohortRows = cohorts.data?.cohorts ?? []

  // Consume a Zone D hand-off once: open the prefilled form, then clear it
  // upstream so a later manual visit doesn't re-open it. `onPrefillConsumed`
  // is a stable useCallback in App, so this settles in one pass.
  useEffect(() => {
    if (prefillSignalId !== null) {
      setLogging(prefillSignalId)
      onPrefillConsumed()
    }
  }, [prefillSignalId, onPrefillConsumed])

  const bump = () => setLocalBump((b) => b + 1)

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          CANDIDATES
          <span className="panel-caption">
            <HelpTerm term="Digest">digest</HelpTerm> order · the <HelpTerm term="the surfaced three">surfaced three</HelpTerm> match the email · prices as of <HelpTerm term="last close">last close</HelpTerm>
            {picks.data?.run_date != null && ` · run ${picks.data.run_date}`}
            {picks.data !== null && ` · quotes ${fmtClock(picks.data.quotes_as_of)}`}
          </span>
          <span className="spacer" />
          <Segmented
            title="pick view"
            options={VIEWS.map((v) => ({
              value: v,
              label: v === 'digest' ? 'digest' : 'with extras',
            }))}
            value={view}
            onChange={setView}
          />
        </div>
        <PanelBody polled={picks} noun="picks">
          {(data) => {
            const empty =
              data.daily.length === 0 &&
              data.reversal.length === 0 &&
              data.extras.length === 0
            if (empty) {
              return (
                <div className="panel-wait">
                  no picks today — next evening screen ~18:05 ET
                </div>
              )
            }
            return (
              <div className="pk-sections">
                <PickSection
                  title="DAILY · continuation"
                  picks={data.daily}
                  cohortRows={cohortRows}
                  onLog={setLogging}
                  emptyNote="no daily picks surfaced this run"
                />
                <PickSection
                  title="REVERSAL"
                  picks={data.reversal}
                  cohortRows={cohortRows}
                  onLog={setLogging}
                  emptyNote="no reversal picks surfaced this run"
                />
                {view === 'all' && (
                  <PickSection
                    title="FLAGGED EXTRAS"
                    caption="liveness-dropped from the digest — shown-and-flagged, never counted in the surfaced three"
                    picks={data.extras}
                    cohortRows={cohortRows}
                    onLog={setLogging}
                    isExtra
                    emptyNote="nothing was dropped this run"
                  />
                )}
                {view === 'digest' && data.extras.length > 0 && (
                  <div className="pk-extra-hint">
                    {data.extras.length} pick{data.extras.length > 1 ? 's' : ''} dropped
                    for liveness — switch to “with extras” to see{' '}
                    {data.extras.length > 1 ? 'them' : 'it'} (they never took a
                    surfaced slot)
                  </div>
                )}
              </div>
            )
          }}
        </PanelBody>
      </section>

      {logging !== null && (
        <section className="panel">
          <div className="panel-head">
            LOG TRADE
            <span className="panel-caption">
              prefilled from the pick’s engine plan · records a fill, places no order
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="pk-log-close"
              onClick={() => setLogging(null)}
            >
              close
            </button>
          </div>
          <LogTradeForm key={logging} initialSignalId={logging} onLogged={bump} />
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <HelpTerm term="reversal funnel">REVERSAL FUNNEL</HelpTerm>
          <span className="panel-caption">latest daily digest</span>
        </div>
        <PanelBody polled={funnel} noun="funnel">
          {(data) =>
            data.funnel === null ? (
              <div className="panel-wait">
                no funnel recorded yet — accrues from the next daily digest
              </div>
            ) : (
              <FunnelBar funnel={data.funnel} />
            )
          }
        </PanelBody>
      </section>
    </main>
  )
}
