/* Typed mirror of the cockpit API contract (src/swing_screener/cockpit/api.py).
   Every statistic arrives as a full Stat — there is deliberately NO helper here
   that returns or formats a bare number (design rule 1). */

import { useEffect, useState } from 'react'

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

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
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

export interface Polled<T> {
  data: T | null
  error: string | null
  /** Wall-clock time of the last SUCCESSFUL fetch (feeds the data-as-of clock). */
  lastFetched: Date | null
}

/** Poll `fetcher` every `ms` (first fetch immediately). Errors — 503s, network —
 * land in `error` and never crash the tree; the last good `data` is kept so the
 * shell stays alive while the per-panel error line shows. */
export function usePolling<T>(fetcher: () => Promise<T>, ms: number): Polled<T> {
  const [state, setState] = useState<Polled<T>>({
    data: null,
    error: null,
    lastFetched: null,
  })

  useEffect(() => {
    let alive = true
    const tick = () => {
      fetcher().then(
        (data) => {
          if (alive) setState({ data, error: null, lastFetched: new Date() })
        },
        (err: unknown) => {
          if (alive)
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
  }, [fetcher, ms])

  return state
}
