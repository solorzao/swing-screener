import { POLL_MS, getWeather, usePolling } from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type { Weather, WeatherResponse } from '../lib/api'
import { Markdown } from '../components/Markdown'
import { PanelBody } from '../components/PanelBody'

/* Screen 8 — Market Weather (plan Task 20): the latest weekly MarketReport plus
   the flip-log history. This is a MARKET-WEATHER read, NOT a stock pick — the
   caption says so, and nothing here surfaces or scores a ticker.

   HONESTY POSTURE:
   - is_deep=false marks the DETERMINISTIC FALLBACK report (no LLM ran) — badged
     so a canned read is never mistaken for the analyst's; is_deep=true badges the
     deep analyst read.
   - every indicator renders its REAL wire value (VIX / yields / bonds / breadth /
     credit / recession); a null renders an em dash (dashOr), never a fabricated 0.
     These are engine facts (levels, ratios, percentiles) — they ride PLAIN, not
     as Stats (no n / CI / provenance exists for a weekly macro snapshot).
   - the report md is served VERBATIM and rendered with the reusable Markdown,
     both opt-outs OFF (stripFrontmatter / strikeFalsifiedH2 false) so a weather
     report opening with `---` or a "Falsified" heading is never swallowed/struck.
   - history is NEWEST-FIRST off the wire and is NEVER re-sorted here.
   - weather null = no report has ever run (weekly job; a fresh DB has none) — a
     setup state, not an error. */

const MINUS = '−'

/** A raw wire number → fixed-decimal string with an optional unit suffix; null
 * renders an em dash (never a fabricated 0). Negatives use the typographic minus
 * to match the rest of the cockpit. */
function numOr(v: number | null, digits: number, suffix = ''): string {
  if (v === null) return '—'
  const s = Math.abs(v).toFixed(digits)
  return `${v < 0 ? MINUS : ''}${s}${suffix}`
}

/** A signed raw wire number (4-week changes carry a real direction) → em dash for
 * null, else an explicit +/− and the unit. */
function signedNumOr(v: number | null, digits: number, suffix = ''): string {
  if (v === null) return '—'
  return `${v < 0 ? MINUS : '+'}${Math.abs(v).toFixed(digits)}${suffix}`
}

/** A nullable string cell (bond_trend, spy_vs_200dma, …) — em dash for null. */
function strOr(v: string | null): string {
  return v === null || v === '' ? '—' : v
}

function IndicatorCell({ label, value, flag }: { label: string; value: string; flag?: string }) {
  return (
    <div className="wx-cell">
      <span className="wx-cell-lab">{label}</span>
      <span className="wx-cell-val mono">
        {value}
        {flag !== undefined && <span className="wx-flag">{flag}</span>}
      </span>
    </div>
  )
}

/** The deterministic indicator grid — VIX complex, yields, credit, cyc/def,
 * breadth, recession. Percentile fields (vix_rank, credit_pctile) are 0-100
 * ranks; recession_prob is a 0-100 probit %; yields are Yahoo %-quotes; the
 * 4-week Δ fields ride signed and raw (the wire's own scale, no unit assumed). */
function Indicators({ w }: { w: Weather }) {
  return (
    <div className="wx-grid">
      <IndicatorCell
        label="VIX"
        value={numOr(w.vix, 2)}
        flag={w.vix_spike ? 'spike' : undefined}
      />
      <IndicatorCell label="VIX rank" value={numOr(w.vix_rank, 0, ' pctile')} />
      <IndicatorCell
        label="VIX term ratio"
        value={numOr(w.vix_term_ratio, 2)}
        flag={w.vix_backwardation ? 'backwardation' : undefined}
      />
      <IndicatorCell label="SPY vs 200dma" value={strOr(w.spy_vs_200dma)} />
      <IndicatorCell label="vol bucket" value={strOr(w.vol_bucket)} />
      <IndicatorCell
        label="10y / 3m"
        value={`${numOr(w.ten_year, 2)} / ${numOr(w.three_month, 2)}`}
        flag={w.yield_inverted === true ? 'inverted' : undefined}
      />
      <IndicatorCell label="bond trend" value={strOr(w.bond_trend)} />
      <IndicatorCell
        label="credit 4w Δ"
        value={signedNumOr(w.credit_chg_4w, 2)}
      />
      <IndicatorCell label="credit rank" value={numOr(w.credit_pctile, 0, ' pctile')} />
      <IndicatorCell
        label="cyc/def trend"
        value={strOr(w.cyc_def_trend)}
      />
      <IndicatorCell label="cyc/def 4w Δ" value={signedNumOr(w.cyc_def_chg_4w, 2)} />
      <IndicatorCell label="breadth trend" value={strOr(w.breadth_trend)} />
      <IndicatorCell label="breadth 4w Δ" value={signedNumOr(w.breadth_chg_4w, 2)} />
      <IndicatorCell label="recession prob" value={numOr(w.recession_prob, 0, '%')} />
    </div>
  )
}

/** The one-line read + the alignment / flip chips + the is_deep provenance badge. */
function CoreLine({ w }: { w: Weather }) {
  return (
    <div className="wx-core">
      <div className="wx-core-chips">
        <span className={`wx-align wx-align-${w.ha_alignment}`}>{w.ha_alignment}</span>
        {w.flipped && (
          <span className="wx-flip" title="at least one SPY timeframe flipped this run">
            flipped
          </span>
        )}
        {w.is_deep ? (
          <span
            className="wx-deep wx-deep-yes"
            title="the LLM analyst produced this read"
          >
            deep analyst
          </span>
        ) : (
          <span
            className="wx-deep wx-deep-no"
            title="deterministic fallback — no LLM ran; this is the canned indicator read, not the analyst's"
          >
            deterministic fallback
          </span>
        )}
      </div>
      <div className="wx-core-text">{w.core}</div>
    </div>
  )
}

function HistoryTable({ history }: { history: WeatherResponse['history'] }) {
  if (history.length === 0) {
    return <div className="panel-wait">no prior runs recorded</div>
  }
  return (
    <div className="wx-hist-wrap">
      <table className="wx-hist">
        <thead>
          <tr>
            <th>run date</th>
            <th>HA alignment</th>
            <th>flip</th>
            <th>core read</th>
          </tr>
        </thead>
        <tbody>
          {history.map((h) => (
            <tr key={h.run_date}>
              <td className="mono">{h.run_date}</td>
              <td>
                <span className={`wx-align wx-align-${h.ha_alignment}`}>
                  {h.ha_alignment}
                </span>
              </td>
              <td>{h.flipped ? <span className="wx-flip">flipped</span> : '—'}</td>
              <td className="wx-hist-core">{h.core}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function WeatherScreen({ wake }: { wake: number }) {
  const weather = usePolling(getWeather, POLL_MS, wake)

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          <HelpTerm term="Market Weather">MARKET WEATHER</HelpTerm>
          <span className="panel-caption">
            a market-weather read, not a stock pick · weekly cadence (runs Sundays
            ~09:00 ET) · prices as of the run date
          </span>
        </div>
        <PanelBody polled={weather} noun="market weather">
          {(data) =>
            data.weather === null ? (
              <div className="panel-wait">
                no weekly report yet — runs Sundays ~09:00 ET
              </div>
            ) : (
              <div className="wx">
                <div className="wx-meta mono">
                  run {data.weather.run_date}
                  {data.weather.created_at !== null &&
                    ` · generated ${data.weather.created_at}`}
                </div>
                <CoreLine w={data.weather} />
                <Indicators w={data.weather} />
                <div className="wx-report">
                  {data.weather.report.trim() === '' ? (
                    <div className="panel-wait">no report text on this run</div>
                  ) : (
                    <Markdown
                      md={data.weather.report}
                      stripFrontmatter={false}
                      strikeFalsifiedH2={false}
                    />
                  )}
                </div>
              </div>
            )
          }
        </PanelBody>
      </section>

      <section className="panel">
        <div className="panel-head">
          <HelpTerm term="flip log">FLIP LOG</HelpTerm>
          <span className="panel-caption">every run, newest first</span>
        </div>
        <PanelBody polled={weather} noun="weather history">
          {(data) => <HistoryTable history={data.history} />}
        </PanelBody>
      </section>
    </main>
  )
}
