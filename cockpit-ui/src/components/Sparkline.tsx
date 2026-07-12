/* Inline SVG sparkline — the Phase-2 chart posture: a ~60-point polyline needs no
   dependency (plan scope decision 7 — zero new bundle bytes, tokens-only styling).
   The y-domain always includes 0 so the zero-line is a real reference, not a
   floating decoration: for a trailing-expectancy drift the whole read is which
   side of zero the line lives on. Renders nothing for an empty series — the
   caller owns empty-state copy.

   Sizing: `width`/`height` define the viewBox COORDINATE SPACE (and thus the
   aspect ratio), not rendered pixels — the SVG carries no width/height
   attributes, and .sparkline's CSS stretches it to its container (Phase 3:
   the fixed-pixel debt paid; a card at 285px and one at 500px both fill). */

export function Sparkline({
  points,
  width = 150,
  height = 28,
}: {
  /** (ISO date, value) pairs, ascending; dates space evenly (index, not time). */
  points: [string, number][]
  width?: number
  height?: number
}) {
  if (points.length === 0) return null

  const pad = 3
  const values = points.map(([, v]) => v)
  const yMin = Math.min(0, ...values)
  const yMax = Math.max(0, ...values)
  const span = yMax - yMin || 1 // all-zero series: flat line, not NaN
  const x = (i: number) =>
    points.length === 1
      ? width / 2
      : pad + (i * (width - 2 * pad)) / (points.length - 1)
  const y = (v: number) => pad + ((yMax - v) * (height - 2 * pad)) / span

  const line = points
    .map(([, v], i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join(' ')
  const lastValue = values[values.length - 1]

  return (
    <svg
      className="sparkline"
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
    >
      <line
        x1={0}
        y1={y(0)}
        x2={width}
        y2={y(0)}
        stroke="var(--dim)"
        strokeWidth={0.5}
      />
      {points.length > 1 && (
        <polyline points={line} fill="none" stroke="var(--blue)" strokeWidth={1.2} />
      )}
      <circle cx={x(points.length - 1)} cy={y(lastValue)} r={2} fill="var(--blue)" />
    </svg>
  )
}
