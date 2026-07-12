import { useState } from 'react'
import { POLL_MS, getPositions, usePolling } from '../lib/api'
import type { PositionRow, Positions, RealPositionRow } from '../lib/api'
import { BracketLamp } from '../components/BracketLamp'
import { CapGauge } from '../components/CapGauge'
import { CloseTradeForm } from '../components/CloseTradeForm'
import { LogTradeForm } from '../components/LogTradeForm'
import { PanelBody } from '../components/PanelBody'
import { Sparkline } from '../components/Sparkline'

/* Screen 3 — Positions & Ledger (plan Task 16): the open table (real + live
   rows, per-row degradation), the two trade forms, the cap gauges, and the
   closed ledger with its realized-equity sparkline.

   Wire-truth posture, row by row:
   - a row with `pl: null` KEEPS its badge and quote — only the P/L cells go
     to dashes (per-row degradation; one malformed trade never blanks the zone);
   - live rows render R-multiples even when dollar P/L is null (no ExecutionLog
     shares join) — R comes from the persisted per-share risk;
   - `override` renders VERBATIM and only when non-null; a null `signal_id`
     renders the "unlinked (manual)" tag — the two markers disambiguate
     engine-faithful from never-prefilled;
   - money cells render exactly what the wire says: null is a dash, never 0.

   The positions poll lives HERE (per-screen; it dies with the screen). After
   a successful log/close the acting form bumps `localBump`, which rides
   usePolling's wake param — an immediate refetch WITHOUT the paramsKey blank
   (scope decision 12's local key-bump); the server-side action nonce + SSE
   cover every other window. The close form mounts OUTSIDE the open table so
   the closed row vanishing on refetch cannot unmount its result panel. */

const fmtPrice = (v: number | null): string => (v === null ? '—' : `$${v.toFixed(2)}`)

const fmtSize = (v: number | null): string =>
  v === null ? '—' : v % 1 === 0 ? v.toFixed(0) : v.toFixed(2)

const fmtSignedUsd = (v: number | null): string =>
  v === null ? '—' : `${v < 0 ? '−' : '+'}$${Math.abs(v).toFixed(2)}`

const fmtPct = (v: number | null): string =>
  v === null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

const fmtR = (v: number | null): string =>
  v === null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`

const fmtAsOf = (iso: string): string => {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString('en-US', { hour12: false })
}

const BADGE_TITLE = {
  red: 'price at/under the stop',
  yellow: 'price at/over the target',
  green: 'between stop and target',
  unknown: 'no quote — state unknown',
} as const

function OpenRow({
  row,
  onClose,
}: {
  row: PositionRow
  onClose: (row: RealPositionRow) => void
}) {
  const pl = row.pl
  const plTitle = pl === null ? 'P/L not computable for this row' : undefined
  return (
    <tr>
      <td>
        <span
          className={`plamp plamp-${row.badge}`}
          title={BADGE_TITLE[row.badge]}
          aria-hidden="true"
        />
        <span className="vh">{BADGE_TITLE[row.badge]}</span>
      </td>
      <td className="pos-name mono">
        {row.ticker}
        <span className="pos-tf"> {row.timeframe}</span>
      </td>
      <td>{row.kind === 'live' ? <span className="pos-tag">live</span> : 'real'}</td>
      <td className="pos-num">{fmtPrice(row.entry_price)}</td>
      <td className="pos-num">{fmtSize(row.size)}</td>
      <td className="pos-num">{fmtPrice(row.stop)}</td>
      <td className="pos-num">{fmtPrice(row.target)}</td>
      <td className="pos-num">{fmtPrice(row.last_close)}</td>
      <td className="pos-num" title={plTitle}>
        {fmtSignedUsd(pl?.unrealized_pl ?? null)}
      </td>
      <td className="pos-num" title={plTitle}>
        {fmtPct(pl?.unrealized_pct ?? null)}
      </td>
      <td className="pos-num" title={plTitle}>
        {fmtR(pl?.r_multiple ?? null)}
      </td>
      <td className="pos-num" title={plTitle}>
        {fmtPct(pl?.dist_to_stop_pct ?? null)}
      </td>
      <td>
        <BracketLamp state={row.bracket} />
      </td>
      <td>
        <div className="pos-flags">
          {row.unlinked && (
            <span
              className="pos-tag"
              title="no signal_id — logged by hand, never prefilled from a pick"
            >
              unlinked (manual)
            </span>
          )}
          {row.kind === 'live' && row.entry_price === null && (
            <span className="pos-tag" title="resting limit unfilled at the broker">
              pending entry
            </span>
          )}
          {row.override !== null && (
            <span
              className="pos-override"
              title="how the logged fill deviated from the engine's plan — stamped server-side at insert"
            >
              {row.override}
            </span>
          )}
        </div>
      </td>
      <td>
        {row.kind === 'real' ? (
          <button type="button" className="pos-close" onClick={() => onClose(row)}>
            CLOSE
          </button>
        ) : (
          <span className="pos-noaction" title="broker-owned row — closes come from the reconciler, not this form">
            —
          </span>
        )}
      </td>
    </tr>
  )
}

export function PositionsScreen({ wake }: { wake: number }) {
  // Local key-bump refetch: rides the wake param (immediate tick, NO data
  // blank), summed with the SSE counter — both only ever increase.
  const [localBump, setLocalBump] = useState(0)
  const positions = usePolling(getPositions, POLL_MS, wake + localBump)
  const [closing, setClosing] = useState<RealPositionRow | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const bump = () => setLocalBump((b) => b + 1)

  return (
    <main className="grid-single">
      {notice !== null && (
        <div className="pos-notice" role="status">
          <span>{notice}</span>
          <button type="button" className="pos-close" onClick={() => setNotice(null)}>
            dismiss
          </button>
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          OPEN POSITIONS
          <span className="panel-caption">
            real (manual) + live broker rows · prices as of last close
            {positions.data !== null &&
              ` · quotes ${fmtAsOf(positions.data.quotes_as_of)} · ${
                positions.data.broker_as_of === null
                  ? 'no broker snapshot'
                  : `broker ${fmtAsOf(positions.data.broker_as_of)}`
              }`}
          </span>
        </div>
        <PanelBody polled={positions} noun="positions">
          {(data: Positions) =>
            data.open.length === 0 ? (
              <div className="panel-wait">no open positions</div>
            ) : (
              <table className="pos-table">
                <thead>
                  <tr>
                    <th aria-label="state" />
                    <th className="left">ticker</th>
                    <th className="left">book</th>
                    <th>entry</th>
                    <th>size</th>
                    <th>stop</th>
                    <th>target</th>
                    <th>last close</th>
                    <th>P/L $</th>
                    <th>P/L %</th>
                    <th>R</th>
                    <th>to stop</th>
                    <th className="left">bracket</th>
                    <th className="left">flags</th>
                    <th aria-label="actions" />
                  </tr>
                </thead>
                <tbody>
                  {data.open.map((row) => (
                    <OpenRow
                      key={row.kind === 'real' ? `r${row.trade_id}` : `l${row.paper_id}`}
                      row={row}
                      onClose={setClosing}
                    />
                  ))}
                </tbody>
              </table>
            )
          }
        </PanelBody>
      </section>

      {closing !== null && (
        <section className="panel">
          <div className="panel-head">
            CLOSE TRADE
            <span className="panel-caption">
              writes the close + a quiet ExitEvent — the exit job never emails
              about a close you performed here
            </span>
          </div>
          <CloseTradeForm
            key={closing.trade_id}
            row={closing}
            onClosed={bump}
            onConflict={(detail) => {
              setClosing(null)
              setNotice(`trade #${closing.trade_id} (${closing.ticker}): ${detail} — list refreshed`)
              bump()
            }}
            onDismiss={() => setClosing(null)}
          />
        </section>
      )}

      <div className="pos-cols">
        <section className="panel">
          <div className="panel-head">
            LOG TRADE
            <span className="panel-caption">
              records a fill you took — places no order · engine defaults via
              signal id
            </span>
          </div>
          <LogTradeForm onLogged={bump} />
        </section>

        <section className="panel">
          <div className="panel-head">
            HARD CAPS
            <span className="panel-caption">the loss cap is an R threshold, not dollars</span>
          </div>
          <PanelBody polled={positions} noun="cap usage">
            {(data: Positions) => (
              <div className="pos-caps">
                <CapGauge label="daily notional" cap={data.caps.notional} kind="usd" />
                <CapGauge label="daily loss (R)" cap={data.caps.loss_r} kind="r" />
                <CapGauge label="concurrent positions" cap={data.caps.concurrent} kind="count" />
                <div className="sfy-note">
                  account {data.caps.account} ·{' '}
                  {data.caps.run_date === null ? 'no runs yet' : `run ${data.caps.run_date}`}
                </div>
              </div>
            )}
          </PanelBody>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head">
          CLOSED LEDGER
          <span className="panel-caption">newest exit first · realized equity plots dated closes only</span>
        </div>
        <PanelBody polled={positions} noun="closed trades">
          {(data: Positions) => (
            <>
              {data.equity.length > 0 && (
                <div className="pos-equity">
                  <Sparkline points={data.equity} width={600} height={80} />
                  <div className="pos-equity-cap mono">
                    cumulative realized{' '}
                    {fmtSignedUsd(data.equity[data.equity.length - 1][1])} as of{' '}
                    {data.equity[data.equity.length - 1][0]}
                  </div>
                </div>
              )}
              {data.closed.length === 0 ? (
                <div className="panel-wait">no closed trades yet</div>
              ) : (
                <table className="pos-table">
                  <thead>
                    <tr>
                      <th className="left">id</th>
                      <th className="left">ticker</th>
                      <th className="left">entry date</th>
                      <th className="left">exit date</th>
                      <th>entry</th>
                      <th>exit</th>
                      <th>size</th>
                      <th>realized $</th>
                      <th className="left">reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.closed.map((t) => (
                      <tr key={t.trade_id}>
                        <td className="pos-name mono">#{t.trade_id}</td>
                        <td className="pos-name mono">{t.ticker}</td>
                        <td className="pos-num">{t.entry_date}</td>
                        <td className="pos-num">{t.exit_date ?? '—'}</td>
                        <td className="pos-num">{fmtPrice(t.entry_price)}</td>
                        <td className="pos-num">{fmtPrice(t.exit_price)}</td>
                        <td className="pos-num">{fmtSize(t.size)}</td>
                        <td className="pos-num">{fmtSignedUsd(t.realized_usd)}</td>
                        <td className="pos-name">{t.exit_reason ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </PanelBody>
      </section>
    </main>
  )
}
