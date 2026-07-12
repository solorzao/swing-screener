import type { PickAnalyst } from '../lib/api'
import { fmtR } from '../lib/fmt'

/* ConvictionChip — the analyst enrichment on a pick (design's ZONE D chip): the
   grade the analyst assigned this pick TODAY, shown next to THAT grade's own
   scored track record. The honesty rule (design doc + Task 17): never render a
   grade without its scored `(n, mean_r)`, and never a bare mean without its n —
   a grade with no scored history reads "unproven (0 scored)".

   `analyst === null` means no AnalystCall exists for the pick: the chip is
   ABSENT (this returns null; the pick's card renders nothing here). The backend
   ties n and mean_r together — a scored grade has n>0 AND a real mean, an
   unscored one is (0, null) — so `n > 0` is the single proven/unproven gate. */

export function ConvictionChip({ analyst }: { analyst: PickAnalyst | null }) {
  if (analyst === null) return null
  const proven = analyst.n > 0 && analyst.mean_r !== null
  return (
    <span
      className={proven ? 'cvc' : 'cvc cvc-unproven'}
      title={
        proven
          ? `analyst grade “${analyst.grade}” · mean ${fmtR(analyst.mean_r as number)} across ${analyst.n} scored calls of that grade (shadow book)`
          : `analyst grade “${analyst.grade}” · no scored calls of that grade yet — the grade is unproven`
      }
    >
      <span className="cvc-grade">{analyst.grade}</span>
      {proven ? (
        <span className="cvc-track">
          {fmtR(analyst.mean_r as number)} <span className="cvc-n">(n={analyst.n})</span>
        </span>
      ) : (
        <span className="cvc-track cvc-unproven-txt">unproven ({analyst.n} scored)</span>
      )}
    </span>
  )
}
