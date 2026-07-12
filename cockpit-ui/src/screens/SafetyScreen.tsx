import type { ReactNode } from 'react'
import { POLL_MS, getExecutionSafety, getPositions, usePolling } from '../lib/api'
import type { ExecutionSafety, Polled } from '../lib/api'
import { BracketLamp } from '../components/BracketLamp'
import { CapGauge } from '../components/CapGauge'
import { PanelBody } from '../components/PanelBody'

/* Screen 7 — Execution Safety: is real money possible, and why not.

   Color doctrine is INVERTED relative to the rest of the app: this screen
   fronts a real-money venue, so the DANGEROUS states are the lit ones. A lock
   that is OFF (mode not live, real money disallowed) is the SAFE resting state
   and renders calm/dim; a lock that is ON glows amber — attention, the system
   is armed(able). GO on the preflight is the loud state; NO-GO is quiet.
   UNKNOWN is never green and never calm-safe either: it renders dashed.

   The HARD STALE LINE (plan Task 15): on any fetch error the safety read
   force-nulls — every panel drops to its UNKNOWN form. A stale GO (or a stale
   "armed" shield lamp) surviving on screen dimmed-but-legible is worse than
   UNKNOWN, so this screen deliberately does NOT use PanelBody's stale-dim for
   safety-derived panels. The cap gauges are informational (not a GO claim) and
   keep the standard PanelBody treatment. */

function fmtAsOf(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString('en-US', { hour12: false })
}

/** One arming lock: ON = lit amber (attention — armed), OFF = calm dim (safe). */
function LockRow({
  name,
  on,
  onLabel,
  offLabel,
}: {
  name: string
  on: boolean
  onLabel: string
  offLabel: string
}) {
  return (
    <div className="sfy-row">
      <span className="sfy-name">{name}</span>
      {on ? (
        <span className="sfy-armed">{onLabel}</span>
      ) : (
        <span className="sfy-safe">{offLabel}</span>
      )}
    </div>
  )
}

/** The force-null wrapper: renders children only from a LIVE read; an errored
 * or not-yet-fetched read renders the panel's UNKNOWN form instead. The
 * force-null derivation lives HERE, from `polled` alone — a caller can't hand
 * in a stale `safe` that disagrees with the poll it came from. */
function SafetyBody({
  polled,
  children,
}: {
  polled: Polled<ExecutionSafety>
  children: (s: ExecutionSafety) => ReactNode
}) {
  const safe = polled.error === null ? polled.data : null
  if (safe !== null) return <>{children(safe)}</>
  return (
    <div className="sfy-unknown-block">
      {polled.error !== null
        ? `UNKNOWN — safety read failed (${polled.error})`
        : 'waiting for first fetch…'}
    </div>
  )
}

export function SafetyScreen({ wake }: { wake: number }) {
  // Both polls are per-screen — they mount here and die with the screen. The
  // safety endpoint makes a REAL broker network call per request (documented
  // in routers/safety.py), so POLL_MS is the floor; the SSE wake covers the
  // post-DISARM refetch (the real run bumps the action nonce server-side).
  const safety = usePolling(getExecutionSafety, POLL_MS, wake)
  const positions = usePolling(getPositions, POLL_MS, wake)

  // The hard stale line: error present -> everything safety-derived is null.
  const safe = safety.error === null ? safety.data : null

  return (
    <>
      {safety.error !== null && (
        <div className="sfy-banner">
          SAFETY READ FAILED — every lamp below reads UNKNOWN, not safe and not
          unsafe · {safety.error}
        </div>
      )}
      <main className="grid-2">
        <div className="col-stack">
          <section className="panel">
            <div className="panel-head">
              EXECUTION STATUS
              <span className="panel-caption">settings truthiness, not connectivity</span>
            </div>
            <SafetyBody polled={safety}>
              {(s) => (
                <>
                  <div className="sfy-row">
                    <span className="sfy-name">broker</span>
                    {s.broker_configured ? (
                      <span className="sfy-val">configured</span>
                    ) : (
                      <span className="sfy-unknown">
                        not configured (SWING_BROKER unset)
                      </span>
                    )}
                  </div>
                  <div className="sfy-row">
                    <span className="sfy-name">execution mode</span>
                    <span className="sfy-val mono">{s.mode}</span>
                  </div>
                  <div className="sfy-row">
                    <span className="sfy-name">env scope</span>
                    <span className="sfy-val">
                      {s.env_scope}
                      <span className="sfy-note">
                        {' '}
                        — the Azure jobs read their own env; the remote mode flip
                        stays the runbook&apos;s az command
                      </span>
                    </span>
                  </div>
                </>
              )}
            </SafetyBody>
          </section>

          <section className="panel">
            <div className="panel-head">
              ARMING LOCKS
              <span className="panel-caption">off is the safe state — calm by design</span>
            </div>
            <SafetyBody polled={safety}>
              {(s) => (
                <>
                  <LockRow
                    name="execution mode is live"
                    on={s.locks.mode_is_live}
                    onLabel="LIVE"
                    offLabel={`off — mode is ${s.mode}`}
                  />
                  <LockRow
                    name="allow real money"
                    on={s.locks.allow_real_money}
                    onLabel="ALLOWED"
                    offLabel="off"
                  />
                  <LockRow
                    name="autonomy gate"
                    on={s.locks.gate_ready}
                    onLabel="READY"
                    offLabel="not ready"
                  />
                  <div className="sfy-row">
                    <span className="sfy-name">caps mandate</span>
                    {s.caps_mandate.ok ? (
                      <span className="sfy-val">{s.caps_mandate.reason || 'all caps set'}</span>
                    ) : (
                      <span className="sfy-warn">{s.caps_mandate.reason}</span>
                    )}
                  </div>
                </>
              )}
            </SafetyBody>
          </section>

          <section className="panel">
            <div className="panel-head">
              HARD CAPS
              <span className="panel-caption">the loss cap is an R threshold, not dollars</span>
            </div>
            <PanelBody polled={positions} noun="cap usage">
              {(p) => (
                <div className="sfy-caps">
                  <CapGauge label="daily notional" cap={p.caps.notional} kind="usd" />
                  <CapGauge label="daily loss (R)" cap={p.caps.loss_r} kind="r" />
                  <CapGauge label="concurrent positions" cap={p.caps.concurrent} kind="count" />
                  <div className="sfy-note">
                    account {p.caps.account} ·{' '}
                    {p.caps.run_date === null
                      ? 'no runs yet'
                      : `run ${p.caps.run_date}`}
                  </div>
                </div>
              )}
            </PanelBody>
          </section>
        </div>

        <div className="col-stack">
          <section className="panel">
            <div className="panel-head">
              PREFLIGHT
              {safe !== null &&
                (safe.preflight.go ? (
                  <span className="sfy-go">GO — a human may arm</span>
                ) : (
                  <span className="sfy-nogo">NO-GO</span>
                ))}
              <span className="panel-caption">read-only; arms nothing, moves no money</span>
            </div>
            <SafetyBody polled={safety}>
              {(s) => (
                <>
                  {s.preflight.checks.map((c) => (
                    <div key={c.name} className="sfy-check">
                      <span className={c.ok ? 'sfy-mark ok' : 'sfy-mark'}>
                        {c.ok ? '✓' : '✗'}
                      </span>
                      <span className="sfy-cname">
                        {c.name}
                        {!c.critical && <em className="sfy-advisory"> advisory</em>}
                      </span>
                      <span className="sfy-detail">{c.detail}</span>
                    </div>
                  ))}
                </>
              )}
            </SafetyBody>
          </section>

          <section className="panel">
            <div className="panel-head">
              BRACKET SHIELD
              <span className="panel-caption">venue truth — UNKNOWN is never green</span>
            </div>
            <SafetyBody polled={safety}>
              {(s) =>
                s.bracket_shield.known ? (
                  <>
                    {s.bracket_shield.positions.length === 0 ? (
                      <div className="panel-wait">no venue positions</div>
                    ) : (
                      <table className="sfy-shield">
                        <thead>
                          <tr>
                            <th>symbol</th>
                            <th>qty</th>
                            <th>stop protection</th>
                          </tr>
                        </thead>
                        <tbody>
                          {s.bracket_shield.positions.map((row) => (
                            <tr key={row.symbol}>
                              <td className="mono">{row.symbol}</td>
                              <td className="mono">{row.qty}</td>
                              <td>
                                <BracketLamp state={row.state} />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {s.bracket_shield.as_of !== null && (
                      <div className="sfy-note pad">
                        snapshot as of {fmtAsOf(s.bracket_shield.as_of)} · cached ≤60s
                      </div>
                    )}
                  </>
                ) : (
                  <div className="sfy-shield-unknown">
                    <div className="sfy-shield-unknown-head">VENUE STATE UNKNOWN</div>
                    {s.broker_configured
                      ? 'venue read failed or degraded — retried each poll; nothing here is assumed protected'
                      : 'no broker configured — there is no venue to read'}
                  </div>
                )
              }
            </SafetyBody>
          </section>
        </div>
      </main>
    </>
  )
}
