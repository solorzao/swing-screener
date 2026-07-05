import type { Heartbeat } from '../lib/api'

/** Age of the last run: "3d 4h", "2h 37m", "12m", "<1m" — or "—" when never run. */
function fmtAge(lastIso: string | null): string {
  if (lastIso === null) return '—'
  const then = Date.parse(lastIso)
  if (Number.isNaN(then)) return '—'
  let s = Math.max(0, Math.floor((Date.now() - then) / 1000))
  const d = Math.floor(s / 86400)
  s -= d * 86400
  const h = Math.floor(s / 3600)
  s -= h * 3600
  const m = Math.floor(s / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return '<1m'
}

/** Compact duration for the tooltip: whole hours as "24h", else "1.5h", "45m", "30s". */
function fmtDur(secs: number): string {
  if (secs >= 3600 && secs % 3600 === 0) return `${secs / 3600}h`
  if (secs >= 3600) return `${(secs / 3600).toFixed(1)}h`
  if (secs >= 60) return `${Math.round(secs / 60)}m`
  return `${secs}s`
}

export function HeartbeatRail({ beats }: { beats: Heartbeat[] }) {
  return (
    <div>
      {beats.map((b) => (
        <div
          key={b.name}
          className={`hb-row hb-${b.state}`}
          title={`period ${fmtDur(b.period_s)} · grace ${fmtDur(b.grace_s)} · ${b.detail}`}
        >
          <span className="hb-lamp" aria-hidden="true" />
          <span className="hb-name">{b.name}</span>
          <span className="hb-age">{fmtAge(b.last)}</span>
        </div>
      ))}
    </div>
  )
}
