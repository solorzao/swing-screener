import { POLL_MS, getPlaybooks, usePolling } from '../lib/api'
import type { Heartbeat, PlaybookBook, Polled } from '../lib/api'
import { HeartbeatRail } from '../components/HeartbeatRail'
import { PanelBody } from '../components/PanelBody'

/** One playbook's integrity row: sidecar health, md health, drift, cadence.
 * UNKNOWN states render dashed-dim, errors amber — never a fabricated ok. */
function PlaybookIntegrityRow({ book }: { book: PlaybookBook }) {
  const verdictsCell =
    book.verdicts_error === null ? (
      <span className="pbint-cell">{book.verdicts.length} verdicts</span>
    ) : book.verdicts_error === 'missing' ? (
      <span className="pbint-cell">no verdicts yet</span>
    ) : (
      <span className="pbint-cell err">verdicts {book.verdicts_error}</span>
    )
  const drift =
    book.drift === null ? (
      <span className="pbint-unknown">drift unknown</span>
    ) : book.drift.ok ? (
      <span className="pbint-cell">md in sync</span>
    ) : (
      <span
        className="pbint-due"
        title={book.drift.missing
          .map((m) => `${m.token} missing from ${m.tier}`)
          .join('\n')}
      >
        drift · {book.drift.missing.length} missing
      </span>
    )
  return (
    <div className="pbint-row">
      <span className="pbint-name">{book.play_type}</span>
      {verdictsCell}
      {book.md_error !== null && <span className="pbint-cell err">md {book.md_error}</span>}
      {drift}
      <span className="pbint-cell right">
        last reflected {book.frontmatter.last_reflected ?? 'never'}
      </span>
      {book.reflection_due && <span className="pbint-due">reflection due</span>}
    </div>
  )
}

/** Screen 9: the heartbeat rail plus playbook integrity, full-width. The
 * playbooks poll is per-screen — it mounts here and dies with the screen. */
export function SystemsScreen({
  beats,
  wake,
}: {
  beats: Polled<Heartbeat[]>
  wake: number
}) {
  const playbooks = usePolling(getPlaybooks, POLL_MS, wake)
  return (
    <main className="grid-2">
      <section className="panel">
        <div className="panel-head">SYSTEMS</div>
        <PanelBody polled={beats} noun="heartbeats">
          {(data) => <HeartbeatRail beats={data} />}
        </PanelBody>
      </section>

      <section className="panel">
        <div className="panel-head">
          PLAYBOOK INTEGRITY
          <span className="panel-caption">numbers come from the sidecar, prose from the md</span>
        </div>
        <PanelBody polled={playbooks} noun="playbook integrity">
          {(data) => (
            <>
              {data.books.map((b) => (
                <PlaybookIntegrityRow key={b.play_type} book={b} />
              ))}
            </>
          )}
        </PanelBody>
      </section>
    </main>
  )
}
