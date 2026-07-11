/* Typed mirror of the cockpit API contract (src/swing_screener/cockpit/api.py).
   Every statistic arrives as a full Stat — there is deliberately NO helper here
   that returns or formats a bare number (design rule 1). */

import { useEffect, useRef, useState } from 'react'

/** A statistic with full provenance — the wire form of cockpit/stats.py `Stat`. */
export interface Stat {
  value: number
  n: number
  n_clusters: number
  ci_low: number
  ci_high: number
  cost_level: string | null
  corpus_id: string | null
  facet: string
  unit: string
  thin_clusters: boolean
}

export interface CohortRow {
  key: string
  strength: string | null
  stat: Stat
}

export interface Cohorts {
  cohorts: CohortRow[]
}

export interface Health {
  connected: boolean
  label: string
  error: string | null
  /** True iff the DB URL is Azure — gates the sign-in affordance. */
  azure: boolean
}

export type HeartbeatState = 'up' | 'late' | 'down' | 'unknown'

export interface Heartbeat {
  name: string
  state: HeartbeatState
  /** ISO-8601 timestamp of the last run, or null when the job has never run. */
  last: string | null
  period_s: number
  grace_s: number
  detail: string
}

/** The two ways of counting: `research` (would_surface, the wide grid) vs `gold`
 * (what production actually surfaced). Mirrors the backend `facet` query param. */
export type Facet = 'research' | 'gold'

/** Trailing leaderboard window in days ('all' = no cut). Shadows the DOM global —
 * always `import type { Window }` and name locals `win`, never `window`. */
export type Window = 'all' | '90' | '180' | '365'

export type PlayType = 'all' | 'continuation' | 'reversal'

/** Lifecycle of a registered experiment book — wire form of settlement.py STATES. */
export type SettlementState =
  | 'accruing'
  | 'settled-awaiting-decision'
  | 'futile-awaiting-decision'
  | 'retired'

/** One Forward Books wall card — the wire form of settlement.py `SettlementCard`. */
export interface SettlementCard {
  name: string
  kind: 'arm' | 'variant'
  play_type: string
  state: SettlementState
  n_accrued: number
  n_needed: number | null
  /** Human ETA line, or null once the book stops accruing. */
  eta: string | null
  stopping_rule: string
  registered_sha: string
  registered_at: string
  mde_r: number
  book: Stat
  control: Stat
  delta: Stat
  /** How the delta's CI was built — 'clustered' (variants) | 'iid' (arms). */
  upper_bound_type: 'clustered' | 'iid'
  /** Trailing-expectancy spark points as (ISO date, R) pairs — drift, not cumulative R. */
  spark: [string, number][]
  decision: string | null
}

/** Cards arrive pre-ordered: awaiting-decision → accruing → retired. */
export interface ForwardBooks {
  cards: SettlementCard[]
}

/** The latest daily digest's reversal funnel snapshot. */
export interface Funnel {
  run_date: string
  detected: number
  confirmed: number
  fresh: number
  actionable: number
  surfaced: number
  overflow: string[]
  pool_n: number
  confirmed_only: boolean
  premium_only: boolean
  already_ran_checked: boolean
}

/** `funnel` is null before the first digest records a snapshot — a normal state. */
export interface FunnelResponse {
  funnel: Funnel | null
}

export interface PerformanceKpis {
  expectancy: Stat
  win_rate: number
  fill_rate: number
  /** null when the book has no losers — JSON has no Infinity (the page's ∞ glyph). */
  profit_factor: number | null
  n_closed: number
}

export interface LeaderboardRow {
  variant: string
  stat: Stat
  win_rate: number
  fill_rate: number
  n_total: number
  /** Trust label, worst-first precedence: iid beats thin beats ok. */
  flag: 'iid' | 'thin' | 'ok'
}

export interface ArmRow {
  arm: string
  stat: Stat
  n_pairs: number
  /** Paired delta vs baseline; null on the baseline row (no self-delta). */
  delta: Stat | null
}

export interface BreakdownRow {
  key: string
  stat: Stat
  win_rate: number
  n_closed: number
}

/** The Streamlit Screener Performance page as data — every aggregate a Stat.
 * Degenerate sections arrive as empty lists, never omitted keys: rendering
 * decisions belong to the frontend. */
export interface Performance {
  kpis: PerformanceKpis
  leaderboard: LeaderboardRow[]
  arms: ArmRow[]
  breakdowns: {
    timeframe: BreakdownRow[]
    rank: BreakdownRow[]
    score: BreakdownRow[]
    market_trend: BreakdownRow[]
    market_vol: BreakdownRow[]
  }
  /** Cumulative-R equity curve as (ISO date, R) pairs. */
  equity_curve: [string, number][]
}

/** The advisory autonomy gate + today's analyst spend, as one status object. */
export interface Gate {
  ready: boolean
  countdown: string
  execution_mode: string
  analyst_spend_today_usd: number
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    // The backend 503s with {"detail": "database unreachable (...)"} — surface
    // that one safe line; anything else keeps the bare status.
    let message = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') message = body.detail
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new Error(message)
  }
  return (await res.json()) as T
}

export const getHealth = (): Promise<Health> => fetchJson<Health>('/api/health')

export const getHeartbeats = (): Promise<Heartbeat[]> =>
  fetchJson<Heartbeat[]>('/api/heartbeats')

export const getCohorts = (facet: Facet = 'research'): Promise<Cohorts> =>
  fetchJson<Cohorts>(`/api/stats/cohorts?facet=${facet}`)

export const getForwardBooks = (facet: Facet = 'research'): Promise<ForwardBooks> =>
  fetchJson<ForwardBooks>(`/api/forward-books?facet=${facet}`)

export const getFunnel = (): Promise<FunnelResponse> => fetchJson<FunnelResponse>('/api/funnel')

export const getPerformance = (
  playType: PlayType,
  win: Window, // `window` would shadow the global — `win` everywhere in here
  facet: Facet,
): Promise<Performance> =>
  fetchJson<Performance>(
    `/api/stats/performance?play_type=${playType}&window=${win}&facet=${facet}`,
  )

export const getGate = (): Promise<Gate> => fetchJson<Gate>('/api/gate')

export interface AzureLoginResponse {
  started: boolean
  already_running?: boolean
  error?: string
}

/** Kick off `az login` on the backend. The response only says whether a login
 * process STARTED — recovery is observed via /api/health, never via this call.
 * X-Cockpit is the guard header: it forces cross-origin callers into a failing
 * CORS preflight; same-origin us attaches it trivially. */
export const postAzureLogin = (): Promise<AzureLoginResponse> =>
  fetchJson<AzureLoginResponse>('/api/azure-login', {
    method: 'POST',
    headers: { 'X-Cockpit': '1' },
  })

export interface Polled<T> {
  data: T | null
  error: string | null
  /** Wall-clock time of the last SUCCESSFUL fetch (feeds the data-as-of clock). */
  lastFetched: Date | null
}

/** Poll `fetcher` every `ms` (first fetch immediately). Errors — 503s, network —
 * land in `error` and never crash the tree; the last good `data` is kept so the
 * shell stays alive while the per-panel error line shows.
 *
 * This hook is the polling TEMPLATE every future component copies. Two guarantees:
 * - The latest `fetcher` is held in a ref and the effect depends on `[ms, wake]`
 *   ALONE, so a caller passing an inline lambda re-renders the ref, not the
 *   effect — it can never tear down/restart the interval into a fetch loop.
 * - Every tick carries a sequence number; a slow response that resolves AFTER a
 *   newer tick's response already landed is discarded (and teardown flips `alive`,
 *   killing the old closure's stragglers), so state never moves backwards in time.
 *
 * `wake` (feed it `useEventWake()`) rides the same path: a changed value tears
 * down and re-creates the interval, which fires the immediate first tick — an
 * instant fetch plus a fresh `ms` cadence from the wake moment. `ms` stays the
 * floor, so a dead wake source degrades to plain polling. */
export function usePolling<T>(fetcher: () => Promise<T>, ms: number, wake = 0): Polled<T> {
  const [state, setState] = useState<Polled<T>>({
    data: null,
    error: null,
    lastFetched: null,
  })

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher // always the latest; the effect reads through the ref

  useEffect(() => {
    let alive = true
    let issued = 0 // ticks fired
    let applied = 0 // newest tick whose response has landed in state
    const tick = () => {
      const seq = ++issued
      fetcherRef.current().then(
        (data) => {
          if (!alive || seq < applied) return // a newer response already landed
          applied = seq
          setState({ data, error: null, lastFetched: new Date() })
        },
        (err: unknown) => {
          if (!alive || seq < applied) return
          applied = seq
          setState((prev) => ({
            ...prev,
            error: err instanceof Error ? err.message : String(err),
          }))
        },
      )
    }
    tick()
    const id = setInterval(tick, ms)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [ms, wake])

  return state
}

/** Subscribe to the SSE wake channel (/api/events). Returns a counter that
 * increments on every `change` event — thread it into `usePolling`'s `wake`
 * param to trigger an immediate refetch.
 *
 * Contract (cockpit/api.py): the FIRST event always fires on (re)connect, so an
 * event is only ever a refetch TRIGGER, never evidence something changed. That
 * means mount bumps the counter 0→1 and causes one immediate re-poll shortly
 * after the first — harmless (usePolling's seq guards) and correct: a reconnect
 * may mean missed changes. Deliberately no error handling: EventSource
 * auto-reconnects, and the poll floor covers a dead channel silently — that is
 * the designed degradation, not a UI-worthy fault. The "data as of" clock keeps
 * deriving from `Polled.lastFetched`; this hook only wakes fetches. */
export function useEventWake(): number {
  const [wake, setWake] = useState(0)

  useEffect(() => {
    const es = new EventSource('/api/events')
    es.addEventListener('change', () => setWake((w) => w + 1))
    return () => es.close()
  }, [])

  return wake
}
