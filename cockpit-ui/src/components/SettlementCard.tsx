import type { SettlementCard as SettlementCardData } from '../lib/api'
import { Sparkline } from './Sparkline'
import { StatChip } from './StatChip'

/* One Forward Books wall card. Every statistic on it (delta, book, control) goes
   through StatChip — the card's own numerals are structural counts only
   (n accrued, n needed). The stopping rule renders VERBATIM in the mono block
   with its registration date + sha: the rule IS the point of the card (the solo
   pre-registration-theater mitigation), so it is never truncated. */

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

      {card.spark.length > 0 && (
        <div className="scard-spark">
          <Sparkline points={card.spark} width={260} height={30} />
          <span className="scard-spark-cap">trailing expectancy</span>
        </div>
      )}
    </div>
  )
}
