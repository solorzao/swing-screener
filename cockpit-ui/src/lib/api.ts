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

export const getCohorts = (): Promise<Cohorts> => fetchJson<Cohorts>('/api/stats/cohorts')

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
 * - The latest `fetcher` is held in a ref and the effect depends on `ms` ALONE, so
 *   a caller passing an inline lambda re-renders the ref, not the effect — it can
 *   never tear down/restart the interval into a fetch loop.
 * - Every tick carries a sequence number; a slow response that resolves AFTER a
 *   newer tick's response already landed is discarded, so state never moves
 *   backwards in time. */
export function usePolling<T>(fetcher: () => Promise<T>, ms: number): Polled<T> {
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
  }, [ms])

  return state
}
