import { signalChartUrl } from '../lib/api'
import type { Actionability, PickRow, Stat } from '../lib/api'
import { fmtR } from '../lib/fmt'
import { ConvictionChip } from './ConvictionChip'
import { Lamp } from './Lamp'
import type { LampColor } from './Lamp'
import { LevelRail } from './LevelRail'
import { StatChip } from './StatChip'

/* PickCard — one surfaced (or flagged-extra) candidate. The card is the whole
   Task-17 pick surface: actionability lamp, LevelRail, conviction/cohort chips,
   the is_repeat + conviction-tier tags, a chart affordance, and the ONE row
   action (log-trade-prefilled).

   Honesty posture, top to bottom:
   - the actionability LAMP colors by status (UNKNOWN is never green), and its
     dist_r never renders bare — status + distance travel together;
   - `extended` on a REVERSAL is its NORMAL resting-limit state, so the wording
     drops the alarm (a confirmed reversal closes above its ceiling by design);
     the same status on a continuation IS the chase;
   - the LevelRail copies every price from the wire;
   - the cohort StatChip is the TIER's measured edge, not the pick's score, and
     is simply absent when the facet has no read;
   - `is_extra` marks a liveness-dropped pick — visually distinct, and it never
     consumed one of the surfaced five (the caller keeps it out of that count). */

/** Actionability status → the shared Lamp color. The `.plamp-*` vocabulary
 * (green circle / amber triangle / red square / dashed hollow) is reused via
 * the Lamp primitive so the pick lamp and the positions lamp never drift. */
const ACT_COLOR: Record<Actionability['status'], LampColor> = {
  actionable: 'green',
  extended: 'yellow',
  broken: 'red',
  unknown: 'unknown',
}

function actTitle(status: Actionability['status'], isReversal: boolean): string {
  switch (status) {
    case 'actionable':
      return 'actionable — last close is in or below the entry zone; a fill is still reachable'
    case 'extended':
      return isReversal
        ? 'extended — last close sits above the ceiling, where the resting limit lives; normal for a reversal'
        : 'extended — last close ran above the entry ceiling (the chase the freshness gate guards against)'
    case 'broken':
      return 'broken — last close is at or below the stop; the setup has already failed'
    case 'unknown':
      return 'no usable quote — actionability unknown (never assumed green)'
  }
}

/** The actionability lamp: the shared Lamp (swatch + `.vh` SR label, never
 * title-only silence) bound to the actionability vocabulary, plus the visible
 * status word with its dist_r. */
function ActionabilityLamp({
  act,
  isReversal,
}: {
  act: Actionability
  isReversal: boolean
}) {
  const title = actTitle(act.status, isReversal)
  return (
    <span className="pk-act">
      <Lamp color={ACT_COLOR[act.status]} title={title} />
      <span className={`pk-act-txt ltf-act-${act.status}`}>
        {act.status}
        {act.dist_r !== null && (
          <span
            className="pk-act-dist"
            title="distance from the entry ceiling in zone-risk units; > 0 = ran above the buy area"
          >
            {' '}
            · {fmtR(act.dist_r)} vs ceiling
          </span>
        )}
      </span>
    </span>
  )
}

export function PickCard({
  pick,
  cohortStat,
  isExtra = false,
  onLog,
}: {
  pick: PickRow
  /** The client-side cohorts join (resolveCohortStat); null → no cohort chip. */
  cohortStat: Stat | null
  isExtra?: boolean
  /** The one row action. Omitted → no LOG button (a read-only surface). */
  onLog?: (signalId: number) => void
}) {
  const isReversal = pick.play_type === 'reversal'
  const cohortLabel = `cohort · ${pick.cohort.strength ?? pick.cohort.play_type}`
  return (
    <div className={isExtra ? 'pk pk-extra' : 'pk'}>
      <div className="pk-head">
        <ActionabilityLamp act={pick.actionability} isReversal={isReversal} />
        <span className="pk-ticker mono">{pick.ticker}</span>
        <span className="pk-meta mono">
          {pick.play_type} · {pick.timeframe} · #{pick.rank}
        </span>
        <span className="pk-spacer" />
        {isExtra && (
          <span
            className="pk-flag"
            title="liveness-dropped from the digest — flagged, never counted in the surfaced five"
          >
            dropped
          </span>
        )}
      </div>

      <div className="pk-tags">
        <span className="pk-tag" title="the engine's conviction tier for this setup">
          {pick.conviction_tier}
        </span>
        {pick.is_repeat && (
          <span
            className="pk-tag pk-repeat"
            title="this setup's streak started on an earlier run — a repeat, not a fresh trigger"
          >
            repeat
          </span>
        )}
        <ConvictionChip analyst={pick.analyst} />
      </div>

      <LevelRail
        floor={pick.entry_floor}
        ceiling={pick.entry_ceiling}
        stop={pick.stop}
        target={pick.target}
        lastClose={pick.last_close}
      />

      <div className="pk-cohort">
        {cohortStat !== null ? (
          <StatChip stat={cohortStat} label={cohortLabel} />
        ) : (
          <span className="pk-nocohort">no cohort read on this facet yet</span>
        )}
      </div>

      <div className="pk-foot">
        {pick.has_chart ? (
          <a
            className="pk-chart"
            href={signalChartUrl(pick.signal_id)}
            target="_blank"
            rel="noreferrer"
            title="signal chart — opens the server byte proxy (a 404 here is normal: the chart may have aged out)"
          >
            <img
              className="pk-chart-thumb"
              src={signalChartUrl(pick.signal_id)}
              alt={`${pick.ticker} signal chart`}
              loading="lazy"
              onError={(e) => {
                // 404 = the chart aged out / was never rendered; hide the broken
                // thumb but keep the link — clicking still hits the proxy.
                e.currentTarget.style.display = 'none'
              }}
            />
            <span className="pk-chart-cap">chart ↗</span>
          </a>
        ) : (
          <span
            className="pk-nochart"
            title="no chart was rendered for this signal — normal; most signals are chartless"
          >
            no chart
          </span>
        )}
        <span className="pk-spacer" />
        {onLog !== undefined && (
          <button
            type="button"
            className="pk-log"
            onClick={() => onLog(pick.signal_id)}
            title="log a trade prefilled from this pick's engine plan (records a fill, places no order)"
          >
            LOG
          </button>
        )}
      </div>
    </div>
  )
}
