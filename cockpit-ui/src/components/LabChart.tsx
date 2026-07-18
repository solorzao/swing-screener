import type { LabBarsPayload } from '../lib/api'

/* The Ticker Lab chart — Heiken Ashi candles with toggleable overlays (EMA
   9/21/50/200, swing-pivot S/R, Fibonacci retracement, last close) plus volume
   and MACD sub-panes sharing the x scale. Hand-rolled SVG like GexProfileChart:
   no chart lib, tokens.css colors only, native SVG <title> tooltips.

   Every number renders VERBATIM from the wire (the LevelRail contract: the
   client derives pixels, never prices). Nulls are honest absences: an EMA's
   warm-up prefix simply has no line, a level outside the visible price range is
   FILTERED, not squeezed in by stretching the domain (the gex level posture). */

export interface LabShow {
  e9: boolean
  e21: boolean
  e50: boolean
  e200: boolean
  vol: boolean
  macd: boolean
  sr: boolean
  fib: boolean
}

const W = 960
const PAD_L = 56 // price ticks
const PAD_R = 100 // level labels
const PAD_T = 10
const MAIN_H = 300
const VOL_H = 60
const MACD_H = 84
const PANE_GAP = 10
const X_AXIS_H = 18

const fin = (v: number | null | undefined): v is number =>
  v !== null && v !== undefined && Number.isFinite(v)

/** Polyline path over aligned values; the pen lifts across nulls, so a warm-up
 * prefix draws nothing rather than a fabricated segment. */
function seriesPath(
  vals: (number | null)[],
  xc: (i: number) => number,
  y: (v: number) => number,
): string {
  let d = ''
  let pen = false
  vals.forEach((v, i) => {
    if (!fin(v)) {
      pen = false
      return
    }
    d += `${pen ? 'L' : 'M'}${xc(i).toFixed(1)} ${y(v).toFixed(1)}`
    pen = true
  })
  return d
}

function fmtPrice(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Math.abs(v) >= 100) return v.toFixed(1)
  return v.toFixed(2)
}

function fmtVol(v: number): string {
  const a = Math.abs(v)
  if (a >= 1e9) return `${(v / 1e9).toFixed(1)}B`
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `${(v / 1e3).toFixed(0)}K`
  return v.toFixed(0)
}

/** Compact x-tick label per timeframe: months keep the year, the rest read MM-DD. */
function tickLabel(t: string, timeframe: string): string {
  return timeframe === '1mo' ? t.slice(0, 7) : t.slice(5, 10)
}

interface HLine {
  price: number
  label: string
  cls: string
}

export function LabChart({ data, show }: { data: LabBarsPayload; show: LabShow }) {
  const candles = data.candles
  const n = candles.length
  if (n === 0) return null

  const innerW = W - PAD_L - PAD_R
  const bw = innerW / n
  const xc = (i: number) => PAD_L + i * bw + bw / 2
  const bodyW = Math.max(1, Math.min(14, bw * 0.66))

  // Price domain: the HA candles plus every TOGGLED EMA value — level lines are
  // filtered to this window, never allowed to stretch it (a far fib endpoint
  // would flatten the candles into a ribbon).
  const domainVals: number[] = []
  for (const c of candles) {
    if (fin(c.ha_l)) domainVals.push(c.ha_l)
    if (fin(c.ha_h)) domainVals.push(c.ha_h)
  }
  const emaFlags: Record<'9' | '21' | '50' | '200', boolean> = {
    '9': show.e9,
    '21': show.e21,
    '50': show.e50,
    '200': show.e200,
  }
  for (const span of ['9', '21', '50', '200'] as const) {
    if (!emaFlags[span]) continue
    for (const v of data.emas[span]) if (fin(v)) domainVals.push(v)
  }
  if (fin(data.last_close)) domainVals.push(data.last_close)
  if (domainVals.length === 0) return null
  let lo = Math.min(...domainVals)
  let hi = Math.max(...domainVals)
  const rawSpan = hi - lo
  if (!(rawSpan > 0)) {
    // A flat window (single price) has no drawable range.
    return <div className="panel-wait">no drawable price range in this window</div>
  }
  lo -= rawSpan * 0.03
  hi += rawSpan * 0.03
  const span = hi - lo

  const yMain = (v: number) => PAD_T + ((hi - v) / span) * MAIN_H

  // Sub-pane stacking (toggle-dependent): volume, then MACD, then the x axis.
  const volTop = PAD_T + MAIN_H + PANE_GAP
  const macdTop = volTop + (show.vol ? VOL_H + PANE_GAP : 0)
  const axisTop = macdTop + (show.macd ? MACD_H + PANE_GAP : 0)
  const H = axisTop + X_AXIS_H

  // Volume scale (0-based).
  const vols = candles.map((c) => (fin(c.v) ? c.v : null))
  const volMax = Math.max(...vols.filter(fin), 1e-9)
  const yVol = (v: number) => volTop + VOL_H - (v / volMax) * VOL_H

  // MACD scale: symmetric around zero over every finite value of the three series.
  const macdAll = [...data.macd.macd, ...data.macd.signal, ...data.macd.hist].filter(fin)
  const macdMax = Math.max(...macdAll.map(Math.abs), 1e-9)
  const yMacd = (v: number) => macdTop + MACD_H / 2 - (v / macdMax) * (MACD_H / 2)

  // Overlay level lines, filtered to the visible domain (never stretched in).
  const hlines: HLine[] = []
  if (show.sr) {
    for (const lv of data.levels.resistance) {
      if (fin(lv.price)) {
        hlines.push({
          price: lv.price,
          label: `R ${fmtPrice(lv.price)} ·${lv.touches}`,
          cls: 'lab-lvl-res',
        })
      }
    }
    for (const lv of data.levels.support) {
      if (fin(lv.price)) {
        hlines.push({
          price: lv.price,
          label: `S ${fmtPrice(lv.price)} ·${lv.touches}`,
          cls: 'lab-lvl-sup',
        })
      }
    }
  }
  if (show.fib && data.fib !== null) {
    for (const lv of data.fib.levels) {
      if (fin(lv.price)) {
        hlines.push({
          price: lv.price,
          label: `fib ${(lv.ratio * 100).toFixed(1)}% ${fmtPrice(lv.price)}`,
          cls: 'lab-lvl-fib',
        })
      }
    }
  }
  if (fin(data.last_close)) {
    hlines.push({
      price: data.last_close,
      label: `close ${fmtPrice(data.last_close)}`,
      cls: 'lab-lvl-close',
    })
  }
  const visibleLines = hlines.filter((l) => l.price >= lo && l.price <= hi)

  // Price ticks (left gutter): 5 evenly spaced values.
  const priceTicks = Array.from({ length: 5 }, (_, i) => lo + (span * (i + 0.5)) / 5)
  // X ticks: at most ~8, on real bar positions.
  const tickEvery = Math.max(1, Math.ceil(n / 8))

  const emaClasses: Record<'9' | '21' | '50' | '200', string> = {
    '9': 'lab-ema9',
    '21': 'lab-ema21',
    '50': 'lab-ema50',
    '200': 'lab-ema200',
  }

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="lab-svg"
      role="img"
      aria-label={`${data.ticker} ${data.timeframe} Heiken Ashi candles with the toggled overlays`}
    >
      {/* main pane frame + price ticks */}
      <rect x={PAD_L} y={PAD_T} width={innerW} height={MAIN_H} className="lab-frame" />
      {priceTicks.map((p) => (
        <g key={`pt${p.toFixed(4)}`}>
          <line
            x1={PAD_L}
            x2={PAD_L + innerW}
            y1={yMain(p)}
            y2={yMain(p)}
            className="lab-grid"
          />
          <text x={PAD_L - 6} y={yMain(p) + 3} className="lab-tick" textAnchor="end">
            {fmtPrice(p)}
          </text>
        </g>
      ))}

      {/* Heiken Ashi candles */}
      {candles.map((c, i) => {
        if (!fin(c.ha_o) || !fin(c.ha_c) || !fin(c.ha_h) || !fin(c.ha_l)) return null
        const up = c.ha_c >= c.ha_o
        const top = yMain(Math.max(c.ha_o, c.ha_c))
        const bot = yMain(Math.min(c.ha_o, c.ha_c))
        const x = xc(i)
        return (
          <g key={c.t} className={up ? 'lab-candle-up' : 'lab-candle-down'}>
            <line x1={x} x2={x} y1={yMain(c.ha_h)} y2={yMain(c.ha_l)} className="lab-wick" />
            <rect
              x={x - bodyW / 2}
              y={top}
              width={bodyW}
              height={Math.max(0.75, bot - top)}
              className="lab-body"
            >
              <title>
                {`${c.t} · HA ${fin(c.ha_o) ? fmtPrice(c.ha_o) : '—'}/${fin(c.ha_h) ? fmtPrice(c.ha_h) : '—'}/${fin(c.ha_l) ? fmtPrice(c.ha_l) : '—'}/${fin(c.ha_c) ? fmtPrice(c.ha_c) : '—'}` +
                  ` · real close ${fin(c.c) ? fmtPrice(c.c) : '—'}` +
                  ` · vol ${fin(c.v) ? fmtVol(c.v) : '—'}`}
              </title>
            </rect>
          </g>
        )
      })}

      {/* EMA overlays (pen lifts across the null warm-up) */}
      {(['9', '21', '50', '200'] as const).map((spanKey) => {
        if (!emaFlags[spanKey]) return null
        const d = seriesPath(data.emas[spanKey], xc, yMain)
        return d === '' ? null : (
          <path key={`ema${spanKey}`} d={d} className={`lab-ema ${emaClasses[spanKey]}`} />
        )
      })}

      {/* level lines over the candles; labels in the right gutter */}
      {visibleLines.map((l) => (
        <g key={`${l.cls}${l.price}`}>
          <line
            x1={PAD_L}
            x2={W - PAD_R + 4}
            y1={yMain(l.price)}
            y2={yMain(l.price)}
            className={`lab-lvl ${l.cls}`}
          />
          <text x={W - PAD_R + 8} y={yMain(l.price) + 3} className={`lab-lvl-lab ${l.cls}`}>
            {l.label}
          </text>
        </g>
      ))}

      {/* volume sub-pane */}
      {show.vol && (
        <g>
          <rect x={PAD_L} y={volTop} width={innerW} height={VOL_H} className="lab-frame" />
          <text x={PAD_L + 4} y={volTop + 10} className="lab-pane-lab">
            VOL
          </text>
          <text x={PAD_L - 6} y={volTop + 10} className="lab-tick" textAnchor="end">
            {fmtVol(volMax)}
          </text>
          {candles.map((c, i) => {
            if (!fin(c.v)) return null
            const up = fin(c.ha_o) && fin(c.ha_c) ? c.ha_c >= c.ha_o : true
            return (
              <rect
                key={`v${c.t}`}
                x={xc(i) - bodyW / 2}
                y={yVol(c.v)}
                width={bodyW}
                height={Math.max(0.5, volTop + VOL_H - yVol(c.v))}
                className={up ? 'lab-vol-up' : 'lab-vol-down'}
              >
                <title>{`${c.t} · vol ${fmtVol(c.v)}`}</title>
              </rect>
            )
          })}
        </g>
      )}

      {/* MACD sub-pane */}
      {show.macd && (
        <g>
          <rect x={PAD_L} y={macdTop} width={innerW} height={MACD_H} className="lab-frame" />
          <text x={PAD_L + 4} y={macdTop + 10} className="lab-pane-lab">
            MACD 12·26·9
          </text>
          <line
            x1={PAD_L}
            x2={PAD_L + innerW}
            y1={yMacd(0)}
            y2={yMacd(0)}
            className="lab-grid"
          />
          {candles.map((c, i) => {
            const h = data.macd.hist[i]
            if (!fin(h)) return null
            const y0 = yMacd(0)
            const y1 = yMacd(h)
            return (
              <rect
                key={`h${c.t}`}
                x={xc(i) - bodyW / 2}
                y={Math.min(y0, y1)}
                width={bodyW}
                height={Math.max(0.5, Math.abs(y1 - y0))}
                className={h >= 0 ? 'lab-hist-pos' : 'lab-hist-neg'}
              >
                <title>{`${c.t} · hist ${h.toFixed(3)}`}</title>
              </rect>
            )
          })}
          {seriesPath(data.macd.macd, xc, yMacd) !== '' && (
            <path d={seriesPath(data.macd.macd, xc, yMacd)} className="lab-macd-line" />
          )}
          {seriesPath(data.macd.signal, xc, yMacd) !== '' && (
            <path d={seriesPath(data.macd.signal, xc, yMacd)} className="lab-macd-signal" />
          )}
        </g>
      )}

      {/* x-axis tick labels */}
      {candles.map((c, i) =>
        i % tickEvery === 0 ? (
          <text
            key={`x${c.t}`}
            x={xc(i)}
            y={axisTop + 12}
            className="lab-tick"
            textAnchor="middle"
          >
            {tickLabel(c.t, data.timeframe)}
          </text>
        ) : null,
      )}
    </svg>
  )
}
