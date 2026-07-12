import type { ReactNode } from 'react'
import type { Polled } from '../lib/api'

/* Shared body treatment — the template every panel (and every Task 15-20
   screen) copies. While a fetch error is present but the last good data is
   still on screen, the body dims (.stale) under a "showing last good data"
   line: stale must LOOK different from fresh, not just carry a footnote. With
   no data at all, the plain unavailable/waiting lines stand alone. (The
   masthead takes the harder line and force-nulls stale heartbeats instead —
   see App.tsx.) */
export function PanelBody<T>({
  polled,
  noun,
  children,
}: {
  polled: Polled<T>
  noun: string
  children: (data: T) => ReactNode
}) {
  const { data, error } = polled
  return (
    <>
      {error !== null && (
        <div className="panel-error">
          {data !== null
            ? `showing last good data · ${error}`
            : `${noun} unavailable — ${error}`}
        </div>
      )}
      {data !== null ? (
        <div className={error !== null ? 'stale' : undefined}>{children(data)}</div>
      ) : (
        error === null && <div className="panel-wait">waiting for first fetch…</div>
      )}
    </>
  )
}
