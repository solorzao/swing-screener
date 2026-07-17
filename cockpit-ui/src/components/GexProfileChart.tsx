import type { GexStrike } from '../lib/api'

/* The Net-GEX profile chart — the public-dashboard visual (horizontal dollar-
   gamma bars per strike) drawn from OUR deterministic engine's numbers, with
   the engine's levels as marked lines. Hand-rolled SVG like Sparkline: no chart
   lib, tokens.css colors only.

   Layout: a CONTINUOUS price y-scale over the profile's strike range (bars sit
   at y(strike); level lines land exactly where they belong, between bars if
   need be). X is net dollar gamma, zero axis centered by the largest magnitude:
   positive (calls dominate) bars grow right in green, negative (puts dominate)
   grow left in red — the MenthorQ reading: green = dampening structure,
   red = acceleration fuel. Only levels the engine produced are drawn — a null
   wall/flip simply has no line (never a fabricated level). */

const W = 720
const PAD_L = 64 // strike labels
const PAD_R = 96 // level-line labels
const PAD_T = 10
const PAD_B = 26

/** Dollar gamma, compact ('$1.2B' / '$340M' / '-$8M') — mirrors
 * options/reading.py fmt_gex_dollars; advisory display, server is authority. */
export function fmtGexUsd(v: number): string {
  const sign = v < 0 ? '-' : ''
  const a = Math.abs(v)
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(1)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(0)}M`
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(0)}K`
  return `${sign}$${a.toFixed(0)}`
}

interface Level {
  price: number
  label: string
  cls: string
}

export function GexProfileChart({
  profile,
  spot,
  callWall,
  putWall,
  gammaFlip,
}: {
  profile: GexStrike[]
  spot: number | null
  callWall: number | null
  putWall: number | null
  gammaFlip: number | null
}) {
  if (profile.length < 2) return null

  const rows = [...profile].sort((a, b) => b.strike - a.strike) // high strikes on top
  const nets = rows.map((r) => r.call_gex + r.put_gex)
  const maxAbs = Math.max(...nets.map(Math.abs), 1e-9)

  const hi = rows[0].strike
  const lo = rows[rows.length - 1].strike
  // Bar thickness from typical strike spacing; chart height follows row count.
  const innerH = Math.max(160, Math.min(520, rows.length * 9))
  const H = PAD_T + innerH + PAD_B
  const y = (price: number) => PAD_T + ((hi - price) / (hi - lo)) * innerH
  const x0 = PAD_L + (W - PAD_L - PAD_R) / 2
  const halfW = (W - PAD_L - PAD_R) / 2
  const xw = (net: number) => (Math.abs(net) / maxAbs) * halfW
  const barH = Math.max(2, Math.min(8, (innerH / rows.length) * 0.72))

  const levels: Level[] = []
  if (callWall !== null) levels.push({ price: callWall, label: `call wall ${callWall.toLocaleString()}`, cls: 'gpc-callwall' })
  if (putWall !== null) levels.push({ price: putWall, label: `put support ${putWall.toLocaleString()}`, cls: 'gpc-putwall' })
  if (gammaFlip !== null) levels.push({ price: gammaFlip, label: `flip ${Math.round(gammaFlip).toLocaleString()}`, cls: 'gpc-flip' })
  if (spot !== null) levels.push({ price: spot, label: `spot ${spot.toLocaleString()}`, cls: 'gpc-spot' })

  // Strike tick labels: at most ~10, on real bar positions.
  const tickEvery = Math.max(1, Math.ceil(rows.length / 10))

  return (
    <div className="gpc-wrap">
      <div className="gpc-legend">
        <span className="gpc-key gpc-key-pos">positive GEX (dampens)</span>
        <span className="gpc-key gpc-key-neg">negative GEX (accelerates)</span>
        <span className="gpc-key gpc-callwall">— call wall</span>
        <span className="gpc-key gpc-putwall">— put support</span>
        <span className="gpc-key gpc-flip">— gamma flip</span>
        <span className="gpc-key gpc-spot">— spot</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="gpc-svg"
        role="img"
        aria-label="net dealer gamma by strike, with the engine's levels marked"
      >
        {/* zero axis */}
        <line x1={x0} y1={PAD_T} x2={x0} y2={PAD_T + innerH} className="gpc-axis" />
        {/* bars */}
        {rows.map((r, i) => {
          const net = nets[i]
          const w = xw(net)
          const yy = y(r.strike) - barH / 2
          return (
            <rect
              key={r.strike}
              x={net >= 0 ? x0 : x0 - w}
              y={yy}
              width={Math.max(w, net === 0 ? 0 : 0.75)}
              height={barH}
              className={net >= 0 ? 'gpc-bar-pos' : 'gpc-bar-neg'}
            >
              <title>
                {`${r.strike.toLocaleString()} · net ${fmtGexUsd(net)} (calls ${fmtGexUsd(r.call_gex)} / puts ${fmtGexUsd(r.put_gex)})`}
              </title>
            </rect>
          )
        })}
        {/* strike ticks */}
        {rows.map((r, i) =>
          i % tickEvery === 0 ? (
            <text key={`t${r.strike}`} x={PAD_L - 6} y={y(r.strike) + 3} className="gpc-tick" textAnchor="end">
              {r.strike.toLocaleString()}
            </text>
          ) : null,
        )}
        {/* x-axis magnitude labels */}
        <text x={x0} y={H - 8} className="gpc-tick" textAnchor="middle">0</text>
        <text x={x0 + halfW} y={H - 8} className="gpc-tick" textAnchor="end">{fmtGexUsd(maxAbs)}</text>
        <text x={x0 - halfW} y={H - 8} className="gpc-tick" textAnchor="start">{fmtGexUsd(-maxAbs)}</text>
        {/* level lines, drawn over the bars; labels in the right gutter */}
        {levels
          .filter((l) => l.price >= lo && l.price <= hi)
          .map((l) => (
            <g key={l.cls + l.price}>
              <line
                x1={PAD_L}
                y1={y(l.price)}
                x2={W - PAD_R + 4}
                y2={y(l.price)}
                className={`gpc-level ${l.cls}`}
              />
              <text x={W - PAD_R + 8} y={y(l.price) + 3} className={`gpc-level-lab ${l.cls}`}>
                {l.label}
              </text>
            </g>
          ))}
      </svg>
    </div>
  )
}
