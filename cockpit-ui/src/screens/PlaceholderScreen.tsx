import { screenDef } from '../lib/screens'
import type { ScreenId } from '../lib/screens'

/** A screen Tasks 15-20 will fill in — the Phase-2 "visible commitments, dead
 * controls beat absent ones" posture: the slot exists, named, honest about
 * when it goes live. Title and task number render from the screen registry —
 * the one source of naming truth, never a re-typed literal. */
export function PlaceholderScreen({ id }: { id: ScreenId }) {
  const def = screenDef(id)
  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">{def.title}</div>
        <div className="panel-wait">
          {def.task !== undefined
            ? `not built yet — lands with Task ${def.task} of the Phase 3 plan`
            : 'not built yet'}
        </div>
      </section>
    </main>
  )
}
