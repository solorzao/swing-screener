import { useEffect, useRef, useState } from 'react'
import type { ReactNode, Ref } from 'react'

/* The destructive-action gesture (design rule 3): a HOLD, never a click. A
   simple click does nothing by construction — the fire path runs only off the
   sustained-hold state machine below, and onClick is preventDefault'd dead.

   Mechanics (plan Task 15 / scope decision 1):
   - press starts the hold (pointer, or Space/Enter held on the keyboard) and
     fires `onHoldStart` — DISARM uses this to launch its dry-run preview;
   - the fill animation runs `holdMs` (900ms for DISARM, 400ms for the light
     decision buttons);
   - releasing early, Escape, dragging off the button, blur, or the parent
     flipping `vetoed`/`disabled` ABORTS — `onAbort` fires so the parent can
     drop preview state;
   - a completed hold fires `onFire` — UNLESS `armed === false`, in which case
     the button WAITS (still held, pulsing) and fires the instant `armed`
     flips true. This is DISARM's preview gate: a completed hold waits on the
     dry-run response, it never fires blind. Releasing while waiting still
     aborts — the fire happens only under a live hold. `armed === undefined`
     means ungated (the light decision buttons).

   Accessibility: a visually-hidden live region INSIDE the component announces
   the phase transitions (holding / waiting / fired / aborted), so every
   consumer — Task 18's approve/withdraw included — inherits screen-reader
   narration without wiring its own; parents announce only their OWN content
   (previews, results). `buttonRef` exposes the button so a parent can restore
   focus after a fire's disable/enable round trip (a fire disables the button,
   which would otherwise drop keyboard focus to <body>). */
export function HoldToConfirm({
  holdMs,
  label,
  waitingLabel,
  className,
  title,
  disabled = false,
  armed,
  vetoed = false,
  buttonRef,
  onHoldStart,
  onFire,
  onAbort,
}: {
  holdMs: number
  label: ReactNode
  /** Shown while a completed hold waits on `armed` (falls back to `label`). */
  waitingLabel?: ReactNode
  className?: string
  title?: string
  disabled?: boolean
  /** The external fire gate — see the contract above. */
  armed?: boolean
  /** Flip true to force-abort an in-progress hold (the preview REJECTED —
   * there is nothing trustworthy left to confirm). */
  vetoed?: boolean
  /** The underlying button, for parents that manage focus around fires. */
  buttonRef?: Ref<HTMLButtonElement>
  onHoldStart?: () => void
  onFire: () => void
  onAbort?: () => void
}) {
  const [holding, setHolding] = useState(false)
  const [holdDone, setHoldDone] = useState(false)
  // The screen-reader phase narration — terse, one word per transition.
  const [live, setLive] = useState('')
  const firedRef = useRef(false)

  // Latest callbacks behind a ref so the effects below depend on state alone —
  // an inline-lambda parent must never tear down a running hold timer.
  const cbRef = useRef({ onHoldStart, onFire, onAbort })
  cbRef.current = { onHoldStart, onFire, onAbort }

  const begin = () => {
    if (disabled || holding) return
    firedRef.current = false
    setHoldDone(false)
    setHolding(true)
    setLive('holding')
    cbRef.current.onHoldStart?.()
  }

  const abort = () => {
    if (!holding || firedRef.current) return
    setHolding(false)
    setHoldDone(false)
    setLive('aborted')
    cbRef.current.onAbort?.()
  }
  const abortRef = useRef(abort)
  abortRef.current = abort

  // The hold timer. Cleared on abort (holding flips false) — a released
  // button can never complete its hold from a stale timeout.
  useEffect(() => {
    if (!holding) return undefined
    const id = window.setTimeout(() => setHoldDone(true), holdMs)
    return () => window.clearTimeout(id)
  }, [holding, holdMs])

  // THE fire condition, in one place: held to completion AND the gate (when
  // one exists) is open. firedRef guards the double-fire when both flip in
  // one render.
  useEffect(() => {
    if (!holding || !holdDone || firedRef.current) return
    if (armed === false) return // waiting — keep holding; armed=true fires
    firedRef.current = true
    setHolding(false)
    setHoldDone(false)
    setLive('fired')
    cbRef.current.onFire()
  }, [holding, holdDone, armed])

  // Escape aborts from anywhere, not just button focus (keyboard-cancellable
  // is a plan requirement on this safety-critical control).
  useEffect(() => {
    if (!holding) return undefined
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') abortRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [holding])

  // A veto or a mid-hold disable (the gate poll withdrew broker_configured)
  // kills the hold — never fire off permissions that just evaporated.
  useEffect(() => {
    if (vetoed || disabled) abortRef.current()
  }, [vetoed, disabled])

  const waiting = holding && holdDone && armed === false

  // The waiting phase is entered by a TIMER, not a user gesture — announce it
  // from state, where the transition actually happens.
  useEffect(() => {
    if (waiting) setLive('waiting')
  }, [waiting])

  const classes = ['htc']
  if (className !== undefined) classes.push(className)
  if (holding) classes.push('htc-holding')
  if (waiting) classes.push('htc-waiting')

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={classes.join(' ')}
        title={title}
        disabled={disabled}
        aria-pressed={holding}
        onPointerDown={(e) => {
          if (e.button === 0) begin()
        }}
        onPointerUp={abort}
        onPointerLeave={abort}
        onKeyDown={(e) => {
          if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
            e.preventDefault() // no synthesized click — the hold is the gesture
            begin()
          }
        }}
        onKeyUp={(e) => {
          if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault()
            abort()
          }
        }}
        onClick={(e) => e.preventDefault()}
        onBlur={abort}
      >
        {/* The fill's transition duration IS the hold duration while holding;
            the snap-back on abort is fast (CSS default). */}
        <span
          className="htc-fill"
          aria-hidden="true"
          style={holding ? { transitionDuration: `${holdMs}ms` } : undefined}
        />
        <span className="htc-body">{waiting ? (waitingLabel ?? label) : label}</span>
      </button>
      {/* The inherited phase narration (see the contract above) — outside the
          button so a disabled button never mutes it. */}
      <span className="vh" role="status">
        {live}
      </span>
    </>
  )
}
