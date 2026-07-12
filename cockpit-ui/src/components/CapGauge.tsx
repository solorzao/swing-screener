import type { CapUsage } from '../lib/api'

/* One hard-limit cap as a gauge (routers/trades.py caps semantics):
   - `limit: null` is UNBOUNDED — "no cap set" rendered on a dashed track,
     deliberately never 0/0 (an unset cap is a POSTURE problem, not zero room);
   - kind 'r' is the daily-loss R THRESHOLD, not a spent-dollars meter: `used`
     is today's realized R with sign preserved and only losses consume the
     budget (the breaker fires at used <= -limit), so a winning day fills 0%;
   - fill turns amber at 80% and red (breach) at 100% — red always pairs with
     the BREACH text, never hue alone. */

const fmtUsd = (v: number): string =>
  `$${v.toLocaleString('en-US', { maximumFractionDigits: 0 })}`

const fmtR = (v: number): string => `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`

export function CapGauge({
  label,
  cap,
  kind,
}: {
  label: string
  cap: CapUsage
  kind: 'usd' | 'r' | 'count'
}) {
  if (cap.limit === null) {
    return (
      <div className="cg">
        <div className="cg-head">
          <span className="cg-label">{label}</span>
          <span className="cg-nocap">no cap set — unbounded</span>
        </div>
        <div className="cg-track cg-indet" />
      </div>
    )
  }

  const consumed = kind === 'r' ? Math.max(0, -cap.used) : cap.used
  const frac = cap.limit > 0 ? consumed / cap.limit : 1
  const level = frac >= 1 ? 'cg-breach' : frac >= 0.8 ? 'cg-warn' : ''
  const text =
    kind === 'usd'
      ? `${fmtUsd(cap.used)} / ${fmtUsd(cap.limit)}`
      : kind === 'r'
        ? `today ${fmtR(cap.used)} · breaker at −${cap.limit.toFixed(2)}R`
        : `${cap.used} / ${cap.limit} open`

  return (
    <div className="cg">
      <div className="cg-head">
        <span className="cg-label">{label}</span>
        <span className="cg-nums">
          {text}
          {frac >= 1 && <b className="cg-breach-tag"> BREACH</b>}
        </span>
      </div>
      <div className="cg-track">
        <div
          className={level === '' ? 'cg-fill' : `cg-fill ${level}`}
          style={{ width: `${Math.min(frac, 1) * 100}%` }}
        />
      </div>
    </div>
  )
}
