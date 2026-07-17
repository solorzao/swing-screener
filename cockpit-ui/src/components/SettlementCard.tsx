import { useState } from 'react'
import { ApiError, postDecideExperiment } from '../lib/api'
import type { ExperimentDecided, SettlementCard as SettlementCardData } from '../lib/api'
import { HelpTerm } from './HelpTerm'
import { HoldToConfirm } from './HoldToConfirm'
import { Sparkline } from './Sparkline'
import { StatChip } from './StatChip'

/* One Forward Books wall card. Every statistic on it (delta, book, control) goes
   through StatChip — the card's own numerals are structural counts only
   (n accrued, n needed). The stopping rule renders VERBATIM in the mono block
   with its registration date + sha: the rule IS the point of the card (the solo
   pre-registration-theater mitigation), so it is never truncated.

   An awaiting-decision card carries the DECIDE box: reason + hold-to-confirm
   retire. Decide MARKS the registry (an uncommitted edge/experiments.json edit,
   audit fields stamped) — the proposals' approve-marks posture applied to
   settlement; the result panel hands back the roster+commit checklist that
   stays human. The SSE nonce re-renders the card retired in every window. */

const STATE_LABEL: Record<SettlementCardData['state'], string> = {
  accruing: 'accruing',
  'settled-awaiting-decision': 'settled — awaiting decision',
  'futile-awaiting-decision': 'futile — awaiting decision',
  retired: 'retired',
}

const STATE_CLS: Record<SettlementCardData['state'], string> = {
  accruing: 'accruing',
  'settled-awaiting-decision': 'awaiting',
  'futile-awaiting-decision': 'awaiting',
  retired: 'retired',
}

/* Horizontal accrual progress with Statsig-style dashed "peek wings" flanking the
   fill edge while the book is still accruing — the dashed zone says "you are
   peeking at an unsettled interval". n_needed null is "cannot project yet", never
   "done" (api.ts contract) — the bar renders indeterminate: dim, no fill. */
function SettlementBar({ card }: { card: SettlementCardData }) {
  if (card.n_needed === null) {
    return <div className="sbar indet" title="too early to project" />
  }
  const pct = Math.min(card.n_accrued / card.n_needed, 1) * 100
  const wing = 10 // peek-wing width, % of track
  const lowerLeft = Math.max(0, pct - wing)
  const upperWidth = Math.min(wing, 100 - pct)
  const iid = card.upper_bound_type === 'iid'
  return (
    <div className="sbar">
      <div className="sbar-fill" style={{ width: `${pct}%` }} />
      {card.state === 'accruing' && (
        <>
          <span
            className="sbar-wing"
            style={{ left: `${lowerLeft}%`, width: `${pct - lowerLeft}%` }}
          />
          <span
            className={iid ? 'sbar-wing upper iid' : 'sbar-wing upper'}
            style={{ left: `${pct}%`, width: `${upperWidth}%` }}
            title={iid ? 'upper bound: IID (unhardened)' : undefined}
          />
        </>
      )}
    </div>
  )
}

type DecidePhase =
  | { kind: 'idle' }
  | { kind: 'firing' }
  | { kind: 'done'; result: ExperimentDecided }
  | { kind: 'error'; detail: string }

/** The decide box (awaiting-decision states only): reason → hold 400 ms →
 * registry marked retired; the result panel is the remaining human half. */
function DecideBox({ name }: { name: string }) {
  const [reason, setReason] = useState('')
  const [phase, setPhase] = useState<DecidePhase>({ kind: 'idle' })
  const busy = phase.kind === 'firing'
  const reasonEmpty = reason.trim() === ''

  if (phase.kind === 'done') {
    const r = phase.result
    return (
      <div className="scard-decide-done" role="status">
        <div className="scard-decide-head">
          RETIRED — {r.name} marked {r.decided_at ?? ''}
        </div>
        <div className="pp-result-note">
          The registry is edited in your working tree (uncommitted). Finish with
          the one commit:
        </div>
        <ol className="pp-checklist">
          {r.checklist.map((step, idx) => (
            <li key={idx}>{step}</li>
          ))}
        </ol>
      </div>
    )
  }

  return (
    <div className="scard-decide">
      {phase.kind === 'error' && (
        <div className="gex-err" role="alert">
          {phase.detail}
        </div>
      )}
      <input
        className="pp-reason"
        value={reason}
        maxLength={200}
        placeholder="decision — why retire? lands verbatim in the registry's audit record"
        aria-label={`decision reason for ${name}`}
        onChange={(e) => setReason(e.target.value)}
      />
      <HoldToConfirm
        holdMs={400}
        className="pp-withdraw"
        disabled={reasonEmpty || busy}
        title={
          reasonEmpty
            ? 'enter a reason first'
            : 'hold 400 ms — marks this experiment retired in edge/experiments.json (an uncommitted working-tree edit); the roster line + commit stay yours'
        }
        label={<span className="pp-hold-label">hold to RETIRE this experiment</span>}
        onFire={() => {
          setPhase({ kind: 'firing' })
          postDecideExperiment(name, reason.trim()).then(
            (result) => setPhase({ kind: 'done', result }),
            (err: unknown) =>
              setPhase({
                kind: 'error',
                detail:
                  err instanceof ApiError
                    ? err.message
                    : 'backend unreachable — the registry may or may not be edited',
              }),
          )
        }}
      />
      <div className="pp-decide-note">
        retiring marks the registry only — the roster line + one commit stay
        yours (the checklist appears after)
      </div>
    </div>
  )
}

export function SettlementCard({ card }: { card: SettlementCardData }) {
  const eta =
    card.n_needed !== null && card.eta !== null
      ? `~${card.n_needed} at current accrual → ${card.eta}`
      : card.n_needed !== null
        ? 'accrual stalled'
        : 'too early to project'

  return (
    <div className={card.state === 'retired' ? 'scard retired' : 'scard'}>
      <div className="scard-head">
        <span className="scard-name">{card.name}</span>
        <span className="scard-kind">
          {card.kind} · {card.play_type}
        </span>
        {/* "Label, don't hide": the IID cue must survive every state — a futile
            verdict rests on exactly this unhardened upper bound. */}
        {card.upper_bound_type === 'iid' && (
          <span
            className="scard-kind"
            title="upper bound: IID (unhardened) — only the lower bound is cluster-hardened"
          >
            iid upper
          </span>
        )}
        <span className={`scard-state ${STATE_CLS[card.state]}`}>
          {STATE_LABEL[card.state]}
        </span>
      </div>

      <SettlementBar card={card} />
      <div className="scard-accrual">
        {/* Structural count, not a statistic — StatChip hides n below 5 by design,
            so the card keeps the accrual count in its own markup. */}
        <span>n accrued: {card.n_accrued}</span>
        <span className="scard-eta">{eta}</span>
      </div>

      <div className="scard-chips">
        <StatChip stat={card.delta} label="Δ vs control" />
        <StatChip stat={card.book} label="book" />
        <StatChip stat={card.control} label="control" />
      </div>

      <pre className="scard-rule">{card.stopping_rule}</pre>
      <div className="scard-meta">
        registered {card.registered_at} · {card.registered_sha.slice(0, 10)}
      </div>
      {card.decision !== null && (
        <div className="scard-decision">decision: {card.decision}</div>
      )}

      {(card.state === 'settled-awaiting-decision' ||
        card.state === 'futile-awaiting-decision') && <DecideBox name={card.name} />}

      {card.spark.length > 0 && (
        <div className="scard-spark">
          <Sparkline points={card.spark} width={260} height={30} />
          <span className="scard-spark-cap">
            <HelpTerm term="trailing expectancy">trailing expectancy</HelpTerm>
          </span>
        </div>
      )}
    </div>
  )
}
