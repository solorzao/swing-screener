import type { BreakdownRow, Performance } from '../lib/api'
import { fmtCountPct } from '../lib/fmt'
import { Segmented } from './Segmented'
import { Sparkline } from './Sparkline'
import { StatChip } from './StatChip'

/* The Streamlit Screener Performance page, cockpit-shaped. Every EXPECTANCY goes
   through StatChip — win/fill rates are percentages of counts and ride as plain
   formatted cells by design (the Stat guards the edge claim, not the tallies);
   n counts are structural. Conditional rendering mirrors the old page exactly:
   leaderboard only with >1 variant, arms only with >1 arm, score section only
   when >1 band has closes. */

/** Breakdown-tab selection. State lives in App next to window/play-type — local
 * state here would be reset by the key-remount blast on every param flip. */
export type BreakdownTab = 'timeframe' | 'rank' | 'score' | 'regime'

function FlagBadge({ flag }: { flag: 'iid' | 'thin' | 'ok' }) {
  if (flag === 'ok') return null // quiet cockpit: healthy rows carry no badge
  return (
    <span
      className={`flag-badge ${flag}`}
      title={
        flag === 'iid'
          ? 'clusters too thin — IID-fallback bound (unhardened)'
          : 'n < 20 — below the leaderboard trust bar (MIN_LEADERBOARD_N)'
      }
    >
      {flag.toUpperCase()}
    </span>
  )
}

function BreakdownTable({ caption, rows }: { caption?: string; rows: BreakdownRow[] }) {
  if (rows.length === 0) return null
  return (
    <table className="perf-table">
      {caption !== undefined && <caption>{caption}</caption>}
      <thead>
        <tr>
          <th />
          <th className="col-stat">expectancy</th>
          <th>win</th>
          <th>n</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td className="cell-name">{r.key}</td>
            <td className="cell-stat">
              <StatChip stat={r.stat} />
            </td>
            <td className="cell-num">{fmtCountPct(r.win_rate)}</td>
            <td className="cell-num">{r.n_closed}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function PerformancePanel({
  data,
  tab,
  onTab,
}: {
  data: Performance
  tab: BreakdownTab
  onTab: (tab: BreakdownTab) => void
}) {
  // The score section exists only when >1 band has closes (page parity); if the
  // data thins out from under a selected score tab, fall back rather than blank.
  const scoreAvailable = data.breakdowns.score.length > 1
  const activeTab = tab === 'score' && !scoreAvailable ? 'timeframe' : tab

  const { kpis } = data
  const tabs: BreakdownTab[] = scoreAvailable
    ? ['timeframe', 'rank', 'score', 'regime']
    : ['timeframe', 'rank', 'regime']

  return (
    <div className="perf">
      <div className="perf-kpis">
        <StatChip stat={kpis.expectancy} label="expectancy" />
        <div className="perf-kpi-cells">
          win {fmtCountPct(kpis.win_rate)} · fill {fmtCountPct(kpis.fill_rate)} · PF{' '}
          {kpis.profit_factor === null ? '∞' : kpis.profit_factor.toFixed(2)} ·{' '}
          {kpis.n_closed} closed
        </div>
      </div>

      {data.leaderboard.length > 1 && (
        <div className="perf-section">
          <div className="perf-section-head">VARIANT LEADERBOARD</div>
          <table className="perf-table">
            <thead>
              <tr>
                <th />
                <th className="col-stat">expectancy</th>
                <th>win</th>
                <th>fill</th>
                <th>n</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.leaderboard.map((r) => (
                <tr key={r.variant}>
                  <td className="cell-name">{r.variant}</td>
                  <td className="cell-stat">
                    <StatChip stat={r.stat} />
                  </td>
                  <td className="cell-num">{fmtCountPct(r.win_rate)}</td>
                  <td className="cell-num">{fmtCountPct(r.fill_rate)}</td>
                  <td className="cell-num">{r.n_total}</td>
                  <td>
                    <FlagBadge flag={r.flag} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.arms.length > 1 && (
        <div className="perf-section">
          <div className="perf-section-head">EXIT ARMS · paired vs baseline</div>
          {data.arms.map((a) => (
            <div key={a.arm} className="arm-row">
              <div className="arm-name">
                {a.arm} <span className="arm-n">n_pairs: {a.n_pairs}</span>
              </div>
              <StatChip stat={a.stat} label="expectancy" />
              {a.delta !== null ? (
                <StatChip stat={a.delta} label="Δ vs baseline" />
              ) : (
                <div className="arm-baseline">baseline — no self-delta</div>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="perf-section">
        <div className="perf-section-head">
          BREAKDOWNS
          <Segmented
            className="perf-tabs"
            options={tabs.map((t) => ({ value: t }))}
            value={activeTab}
            onChange={onTab}
          />
        </div>
        {activeTab === 'timeframe' && (
          <BreakdownTable rows={data.breakdowns.timeframe} />
        )}
        {activeTab === 'rank' && <BreakdownTable rows={data.breakdowns.rank} />}
        {activeTab === 'score' && <BreakdownTable rows={data.breakdowns.score} />}
        {activeTab === 'regime' && (
          <>
            <BreakdownTable caption="market trend" rows={data.breakdowns.market_trend} />
            <BreakdownTable caption="market vol" rows={data.breakdowns.market_vol} />
          </>
        )}
      </div>

      {data.equity_curve.length > 0 && (
        <div className="perf-section">
          <div className="perf-section-head">EQUITY CURVE · cumulative R</div>
          <div className="perf-equity">
            <Sparkline points={data.equity_curve} width={330} height={48} />
          </div>
        </div>
      )}
    </div>
  )
}
