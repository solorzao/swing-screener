import { useRef, useState } from 'react'
import type { ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { lookupTerm } from '../lib/glossary'
import type { TermDef } from '../lib/glossary'
import { useAnchoredPopover } from '../lib/useAnchoredPopover'

/* HelpTerm — an inline, dotted-underlined label that opens a small definition
   popover on click. It answers "what does THIS word mean" in context; the
   glossary panel (screen 10) is the browse-everything surface. The affordance is
   the neutral blue accent, never green/amber (those carry system-state meaning).

   Reuses useAnchoredPopover (the ProvenancePopover portal machinery) so it
   escapes the .panel overflow:hidden clip. If the term isn't in the glossary it
   renders plain text — never a dead affordance. The house rule: HelpTerm wraps a
   LABEL; a StatChip's number keeps its own click→ProvenancePopover. */

let seq = 0

export function HelpTerm({
  term,
  children,
  className,
}: {
  /** Glossary lookup key (exact term, or a substring like "gold"). */
  term: string
  /** Visible label; defaults to the matched term's canonical name. */
  children?: ReactNode
  className?: string
}) {
  const def = lookupTerm(term)
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [id] = useState(() => `helpterm-${(seq += 1)}`)

  // Unknown term: render plain text so a missing definition can never present as
  // a clickable-but-empty affordance.
  if (def === undefined) return <>{children ?? term}</>

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        className={className === undefined ? 'help-term' : `help-term ${className}`}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        {children ?? def.term}
      </button>
      {open && (
        <HelpTermPopover def={def} id={id} triggerRef={triggerRef} onClose={() => setOpen(false)} />
      )}
    </>
  )
}

function HelpTermPopover({
  def,
  id,
  triggerRef,
  onClose,
}: {
  def: TermDef
  id: string
  triggerRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
}) {
  const { ref, pos } = useAnchoredPopover(triggerRef, onClose)
  return createPortal(
    <div
      className="help-pop"
      role="tooltip"
      id={id}
      ref={ref}
      style={{
        top: pos?.top ?? 0,
        left: pos?.left ?? 0,
        visibility: pos === null ? 'hidden' : 'visible',
      }}
    >
      <div className="help-pop-term">
        {def.term}
        <span className="help-pop-cat">{def.cat}</span>
      </div>
      <div className="help-pop-row">
        <span className="help-pop-k">MEANS</span>
        {def.short}
      </div>
      <div className="help-pop-row help-pop-why">
        <span className="help-pop-k">WHY IT MATTERS</span>
        {def.why}
      </div>
      {def.source !== undefined && <div className="help-pop-src">{def.source}</div>}
    </div>,
    document.body,
  )
}
