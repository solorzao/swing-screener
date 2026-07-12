import type { ReactNode } from 'react'
import { dashOr, fmtUsd } from '../lib/fmt'

/* LevelRail — a pick's engine geometry as one horizontal rail: the entry zone
   [floor, ceiling] as a band, the stop and target as ticks, and last_close as
   the live position mark. Every PRICE is COPIED from the wire and shown VERBATIM
   in the label line — LevelRail never recomputes a level. Only the pixel
   positions are derived (the same posture as RiskStrip's distance bar and the
   FunnelBar's proportions), and the label line always carries the real numbers,
   so a mis-scaled bar can never hide the truth.

   The plot domain stretches to include last_close, so a broken pick (below the
   stop) or an extended reversal (above the ceiling) still shows its mark in
   range. A null last_close (quote miss) drops the mark and reads "no quote",
   never a fabricated position. A degenerate zone (levels collapsed) plots
   nothing and leans on the labels alone. */

const price = (v: number | null): string => dashOr(v, (n) => fmtUsd(n))

export function LevelRail({
  floor,
  ceiling,
  stop,
  target,
  lastClose,
}: {
  floor: number
  ceiling: number
  stop: number
  target: number
  lastClose: number | null
}) {
  const lo = Math.min(stop, floor, lastClose ?? Infinity)
  const hi = Math.max(target, ceiling, lastClose ?? -Infinity)
  const span = hi - lo
  const pos = (p: number): number => (span > 0 ? ((p - lo) / span) * 100 : 50)

  let track: ReactNode
  if (span <= 0) {
    track = (
      <div className="lr-track lr-degenerate" title="levels collapsed — nothing to plot" />
    )
  } else {
    const zoneL = pos(Math.min(floor, ceiling))
    const zoneR = pos(Math.max(floor, ceiling))
    track = (
      <div className="lr-track">
        <span className="lr-zone" style={{ left: `${zoneL}%`, width: `${zoneR - zoneL}%` }} />
        <span className="lr-tick lr-stop" style={{ left: `${pos(stop)}%` }} />
        <span className="lr-tick lr-target" style={{ left: `${pos(target)}%` }} />
        {lastClose !== null && (
          <span className="lr-mark" style={{ left: `${pos(lastClose)}%` }} />
        )}
      </div>
    )
  }

  return (
    <div className="lr">
      {track}
      <div className="lr-labels mono">
        <span className="lr-lab lr-lab-stop">stop {price(stop)}</span>
        <span className="lr-lab lr-lab-zone">
          zone {price(floor)}–{price(ceiling)}
        </span>
        <span className="lr-lab lr-lab-target">tgt {price(target)}</span>
        <span className="lr-lab lr-lab-last">
          last {lastClose === null ? 'no quote' : fmtUsd(lastClose)}
        </span>
      </div>
    </div>
  )
}
