import { useEffect, useRef, useState } from 'react'
import {
  ApiError,
  D503_DB,
  POLL_MS,
  getStrategies,
  postGuardrails,
  usePolling,
} from '../lib/api'
import type { GuardrailsPostResult, StrategyRow, Strategies } from '../lib/api'
import { fmtR, fmtStamp } from '../lib/fmt'
import { HelpTerm } from './HelpTerm'
import { HoldToConfirm } from './HoldToConfirm'
import { Lamp } from './Lamp'
import { TierChip } from './TierChip'

/* STRATEGY SCOPE — the Strategy Board, under GUARDRAILS on the Safety screen.

   WHAT IT IS: one row per play type, ranked by EVIDENCE (tier first, then the
   CI floor — the gate's own number, never the point estimate), with the one
   knob the cockpit owns: subtracting a strategy from execution scope.

   THE TWO-LEVEL SCOPE, and the caption states it because it is the whole
   design: `SWING_EXECUTE_PLAY_TYPES` is the CEILING (env/IaC ceremony, always),
   and this board's `disabled_play_types` is a SUBTRACTION under it. Enabling
   here can only ever restore what the ceiling already allows — exactly like
   releasing a HALT returns to what the master arm permits. Nothing here can arm
   what was not already armed.

   THE THREE-STATE TIER LAW: ungraded (`tier: null`) is NOT `hunch`. A missing or
   unreadable verdicts sidecar means we never measured this play type; `hunch`
   means we measured it and it is unproven. Rendering the first as the second is
   a fabricated grade, so null gets its own dashed "ungraded" chip and
   `playbook_present: false` suppresses the tier and cohort cells outright.

   THE None-MEANS-ALL CONTRACT: `effective_scope` / `ceiling` are null for "no
   scoping applies", which means EVERYTHING is in — the falsy-empty check that
   collapses null with `[]` would paint an unscoped board as fully OUT. The
   per-row `effective` boolean is what the lamp reads (the SAME function
   execution enforces), never `in_ceiling`.

   HONESTY RULES, inherited from the screen and from GuardrailsPanel:
   - a failed read force-nulls every CLAIM (the scope header, the lamps, the
     tier, the numbers) to a dashed UNKNOWN. A stale IN SCOPE lamp on a
     real-money screen is the exact failure this doctrine exists to prevent.
   - the CONTROLS keep rendering from the retained last-good snapshot, and the
     toggle RESULT lives at panel level — outside the data swap — so a poll
     failure can never destroy what the operator just did (the Task-16 lesson).
   - a write that 503s with the `database error (` prefix carries the G7 hint:
     the cockpit's write grant may be missing in prod, while the scope itself is
     still ENFORCED by the jobs reading the same row. */

/** A write rejection → one human line + whether to show the G7 write-grant hint.
 * `database error (` is one of the three prefixes routers/safety.py documents as
 * a stable contract; the text after it is human-facing and never matched on. */
interface WriteErr {
  text: string
  g7: boolean
}

function writeError(err: unknown): WriteErr {
  if (err instanceof ApiError) {
    return { text: err.message, g7: err.message.startsWith(D503_DB) }
  }
  return { text: 'backend unreachable — the scope may or may not have changed', g7: false }
}

/** `announce` false when the PARENT is already a live region — a nested
 * role="alert" double-announces the same sentence. */
function ErrLine({ err, announce = true }: { err: WriteErr; announce?: boolean }) {
  return (
    <div className="gr-err" role={announce ? 'alert' : undefined}>
      {err.text}
      {err.g7 && (
        <div className="gr-err-hint">
          write grant (G7) may be missing — execution scope is still enforced by the
          jobs; only the cockpit&apos;s own edit is refused
        </div>
      )}
    </div>
  )
}

/* ---------- the scope header (pure claims) ---------- */

/** A scope list on screen. The null branch is the load-bearing one: null is "no
 * scoping applies" (everything is in), `[]` is "nothing dispatches" — the two
 * ends of the same axis, and a falsy check would render them identically. */
function ScopeLine({
  label,
  value,
  whenNull,
  whenEmpty,
  emptyTone = 'warn',
}: {
  label: string
  value: string[] | null
  whenNull: string
  whenEmpty: string
  /** An empty list means "nothing dispatches" on the two SCOPE lines — worth an
   * amber look, since it is usually an env var failing closed. On the board's
   * own SUBTRACTION line the same `[]` means "nothing switched off", which is
   * the calm resting state; painting that amber would invent an alarm. */
  emptyTone?: 'warn' | 'calm'
}) {
  return (
    <div className="sb-scope-row">
      <span className="sb-scope-k">{label}</span>
      {value === null ? (
        <span className="sb-scope-v sb-scope-all">{whenNull}</span>
      ) : value.length === 0 ? (
        <span
          className={`sb-scope-v ${emptyTone === 'warn' ? 'sb-scope-none' : 'sb-scope-all'}`}
        >
          {whenEmpty}
        </span>
      ) : (
        <span className="sb-scope-v mono">{value.join(' · ')}</span>
      )}
    </div>
  )
}

function ScopeHeader({ s }: { s: Strategies }) {
  return (
    <div className="sb-scope">
      <ScopeLine
        label="ceiling (env)"
        value={s.ceiling}
        whenNull="unset — the env expresses NO ceiling, so every play type is allowed"
        whenEmpty="EMPTY — nothing dispatches (a garbled SWING_EXECUTE_PLAY_TYPES fails closed)"
      />
      <ScopeLine
        label="board subtracts"
        value={s.disabled}
        whenNull="—"
        whenEmpty="nothing — the board has switched nothing off"
        emptyTone="calm"
      />
      <ScopeLine
        label="effective now"
        value={s.effective_scope}
        whenNull="unscoped — every play type is traded (no ceiling, nothing subtracted)"
        whenEmpty="EMPTY — no play type dispatches"
      />
      <div className="sb-asof mono">read {fmtStamp(s.as_of)}</div>
    </div>
  )
}

/* ---------- one strategy row ---------- */

/** IN SCOPE / OUT, read from `effective` and ONLY from `effective` — the same
 * `effective_scope_from_state` answer dispatch filters on, so this lamp can
 * never say TRADED about a play type execution would drop.
 *
 * Colour follows the SCREEN's inverted doctrine, not the app's: in scope means
 * this strategy may reach a real-money venue, so it is the LIT (amber,
 * attention) state; OUT is the calm inert one. Green is never used — on this
 * app green is the forward-confirmed edge claim alone. */
function ScopeLamp({ effective }: { effective: boolean }) {
  return effective ? (
    <span className="sb-lamp sb-in">
      <Lamp
        color="yellow"
        title="IN SCOPE — execution may dispatch this play type right now"
      />
      <span className="sb-lamp-label">IN SCOPE</span>
    </span>
  ) : (
    <span className="sb-lamp sb-out">
      <Lamp
        color="gray"
        title="OUT — execution drops this play type before it reaches the venue"
      />
      <span className="sb-lamp-label">OUT</span>
    </span>
  )
}

/** The advisory autonomy gate's own per-play-type verdict. Amber for READY on
 * this screen (readiness is an ARMED-ward state), dim otherwise — and never
 * green: READY is an operational check, not a claim that an edge exists. */
function GateLamp({ ready }: { ready: boolean }) {
  return (
    <span className={ready ? 'sb-lamp sb-in' : 'sb-lamp sb-out'}>
      <Lamp
        color={ready ? 'yellow' : 'gray'}
        title={
          ready
            ? 'autonomy gate READY for this play type — an operational check, never a claim that an edge exists'
            : 'autonomy gate NOT READY for this play type'
        }
      />
      <span className="sb-lamp-label">{ready ? 'GATE READY' : 'GATE NOT READY'}</span>
    </span>
  )
}

/** The evidence cells: tier + the strongest cell within it.
 *
 * THREE STATES, never two. `playbook_present: false` suppresses both cells
 * outright (prose whose numbers nothing backs is not a playbook); `tier: null`
 * is UNGRADED and wears the dashed unknown chip — it is never rendered as
 * `hunch`, which would turn "never measured" into a grade we never gave. */
function Evidence({ r, ciNote }: { r: StrategyRow; ciNote: string }) {
  if (!r.playbook_present) {
    return (
      <div className="sb-nobook">
        no playbook — edge/{r.play_type}.md and the verdicts sidecar are not both
        present, so there is no tier and no cohort to show
      </div>
    )
  }
  const c = r.best_cohort
  return (
    <>
      <div className="sb-evid">
        {r.tier === null ? (
          <span className="tchip tchip-unknown">
            <Lamp
              color="unknown"
              title="ungraded — no verdicts sidecar was read for this play type. NOT a hunch: never measured is a different claim from measured and unproven."
            />
            <span className="tchip-label">ungraded</span>
          </span>
        ) : (
          <TierChip tier={r.tier} />
        )}
        <GateLamp ready={r.gate_ready} />
      </div>
      <div className="sb-cohort">
        {c === null ? (
          <span className="sb-nm" title="no cohort on the winning rung — not measured, never a zero">
            bound not measured
          </span>
        ) : (
          <>
            {c.ci_low === null ? (
              <span
                className="sb-nm"
                title="the bound is non-finite (an empty bucket) — not a measured number"
              >
                bound not measured
              </span>
            ) : (
              <span className="mono sb-bound" title={ciNote}>
                ≥ {fmtR(c.ci_low)}
              </span>
            )}
            <span className="mono sb-n">n={c.n.toLocaleString('en-US')}</span>
            <span className="mono sb-bucket">
              {c.dimension}={c.bucket}
            </span>
          </>
        )}
      </div>
    </>
  )
}

/* ---------- the toggle ---------- */

type ScopeOutcome =
  | { kind: 'done'; playType: string; disabling: boolean; result: GuardrailsPostResult }
  | { kind: 'failed'; playType: string; disabling: boolean; err: WriteErr }

/** The scope toggle: a 400 ms hold — the LIGHT-decision tier (PlaybooksScreen's
 * approve/withdraw), not HALT's 900 ms. The tiers are not decoration: 900 ms
 * belongs to the gestures that move VENUE state (a sweep, a brake release);
 * this one writes a single DB column and moves no order.
 *
 * The body carries the WHOLE new disabled set, never a delta — the server SETs
 * it, so a client that posted only its own change would wipe every other
 * window's. Whole-set is also why the verb is idempotent and has NO 409: a
 * double press lands the same set twice, which is safe by construction.
 *
 * THE CEILING CAP: with `in_ceiling === false` the control is dead in BOTH
 * directions and says why — the env ceiling already excludes this play type,
 * and widening it is an env/IaC act this board may never perform. `in_ceiling
 * === null` means the env expresses no ceiling at all, so both directions
 * work. */
function ScopeToggle({
  r,
  disabledSet,
  stale,
  onWrote,
  onOutcome,
}: {
  r: StrategyRow
  /** The board's whole stored subtraction, VERBATIM off the wire (stale members
   * included — the repo tolerates re-saving them, and dropping one here would
   * silently re-enable a strategy nobody asked to re-enable). */
  disabledSet: string[]
  stale: boolean
  onWrote: () => void
  onOutcome: (o: ScopeOutcome) => void
}) {
  const [busy, setBusy] = useState(false)
  const capped = r.in_ceiling === false
  const disabling = !r.disabled

  const next = disabling
    ? [...disabledSet, r.play_type].sort()
    : disabledSet.filter((pt) => pt !== r.play_type)

  const fire = () => {
    setBusy(true)
    postGuardrails({ action: 'set_scope', disabled: next }).then(
      (result) => {
        setBusy(false)
        onOutcome({ kind: 'done', playType: r.play_type, disabling, result })
        onWrote()
      },
      (err: unknown) => {
        setBusy(false)
        onOutcome({
          kind: 'failed',
          playType: r.play_type,
          disabling,
          err: writeError(err),
        })
        onWrote()
      },
    )
  }

  return (
    <div className="sb-ctl">
      <HoldToConfirm
        holdMs={400}
        className={disabling ? 'sb-disable' : 'sb-enable'}
        disabled={capped || busy}
        title={
          capped
            ? 'the env ceiling excludes this play type — env ceremony sets the ceiling'
            : disabling
              ? `hold 400 ms to take ${r.play_type} out of execution scope (pure risk removal — it stops dispatching)`
              : `hold 400 ms to restore ${r.play_type} — back up to the env ceiling, never past it`
        }
        label={
          <span className="dz-btn-label">
            <b>
              {busy
                ? 'SAVING…'
                : disabling
                  ? 'hold to DISABLE'
                  : 'hold to RE-ENABLE'}
            </b>
            <small>
              {disabling
                ? 'stops dispatching · positions and stops untouched'
                : 'restores it within the env ceiling'}
            </small>
          </span>
        }
        onFire={fire}
      />
      {capped && (
        <div className="sb-why">
          env ceremony sets the ceiling — SWING_EXECUTE_PLAY_TYPES excludes{' '}
          {r.play_type}, and this board can only subtract or restore WITHIN it.
          {r.disabled &&
            ' (it is also on the board’s subtraction list, which changes nothing while the ceiling excludes it.)'}
        </div>
      )}
      {stale && !capped && (
        <div className="sb-why">
          acting on the last good read — the server SETs the whole set, so a stale
          screen cannot half-apply an edit
        </div>
      )}
    </div>
  )
}

/* ---------- the toggle result (panel-level: it outlives the row) ---------- */

/** What a committed scope write did, from the WIRE — never from what we sent.
 * The state keys are optional by contract: when the post-commit read fails the
 * answer is still 200/committed with `enrichment_error` set and the keys ABSENT,
 * so the disabled set is reported only when the server actually re-read it. */
function ScopeResult({
  outcome,
  onDismiss,
}: {
  outcome: ScopeOutcome
  onDismiss: () => void
}) {
  const dismissRef = useRef<HTMLButtonElement>(null)
  // The hold button is destroyed/re-labelled by the poll that follows — land
  // focus on this panel's own dismiss control instead of dropping it to <body>.
  useEffect(() => {
    dismissRef.current?.focus()
  }, [outcome])

  const verb = outcome.disabling ? 'taken OUT of scope' : 'restored to scope'
  return (
    <div className="gr-pop" role="status">
      <div className="gr-pop-head">
        {outcome.kind === 'done' && outcome.result.committed
          ? 'SCOPE SAVED'
          : 'SCOPE UNCHANGED'}
      </div>
      {outcome.kind === 'done' ? (
        <>
          <div className="gr-row">
            {/* `committed` is the WIRE's word for whether the row moved, not our
                intent. set_scope always commits (there is no dry run on this
                verb) — but reading the flag rather than assuming it means a
                future preview mode cannot turn this sentence into a lie. */}
            {outcome.result.committed
              ? `${outcome.playType} ${verb} — the board's subtraction moved; the env ceiling did not`
              : `${outcome.playType} unchanged — the server reported no commit`}
          </div>
          {outcome.result.disabled_play_types !== undefined && (
            <div className="gr-row mono">
              disabled now:{' '}
              {outcome.result.disabled_play_types.length === 0
                ? '(none)'
                : outcome.result.disabled_play_types.join(', ')}
            </div>
          )}
          {outcome.result.enrichment_error != null && (
            <div className="gr-warn-row">
              the follow-up read failed ({outcome.result.enrichment_error}) — the
              WRITE STANDS; the board re-polls for the fresh scope
            </div>
          )}
        </>
      ) : (
        <>
          <ErrLine err={outcome.err} announce={false} />
          <div className="gr-foot">
            the scope did NOT move — this write is an idempotent whole-set SET, so
            pressing it again is safe
          </div>
        </>
      )}
      <button type="button" className="gr-dismiss" ref={dismissRef} onClick={onDismiss}>
        dismiss
      </button>
    </div>
  )
}

/* ---------- the panel ---------- */

function DataUnknown({ error, noun }: { error: string | null; noun: string }) {
  return (
    <div className="sfy-unknown-block">
      {error !== null
        ? `UNKNOWN — the strategy read failed (${error}). Not "in scope": the ${noun} was not read.`
        : 'waiting for first fetch…'}
    </div>
  )
}

/** One board row. `claims` is null when the poll is failing: the identity and
 * the control still render (destroying a control on a failed poll costs the
 * operator their action), but every TRUTH CLAIM is withheld rather than shown
 * one tick stale. */
function BoardRow({
  r,
  claims,
  ciNote,
  disabledSet,
  onWrote,
  onOutcome,
}: {
  r: StrategyRow
  claims: StrategyRow | null
  ciNote: string
  disabledSet: string[]
  onWrote: () => void
  onOutcome: (o: ScopeOutcome) => void
}) {
  return (
    <div className="sb-row">
      <div className="sb-head">
        <span className="sb-rank mono">#{r.rank}</span>
        <span className="sb-pt">{r.play_type}</span>
        {claims === null ? (
          <span className="sb-lamp sb-unread">
            <Lamp
              color="unknown"
              title="scope not read this poll — not in scope and not out"
            />
            <span className="sb-lamp-label">UNKNOWN</span>
          </span>
        ) : (
          <ScopeLamp effective={claims.effective} />
        )}
      </div>
      {claims === null ? (
        <div className="sb-unread-note">
          evidence withheld — the read failed, and a one-tick-old tier or bound on
          this screen is worse than no number
        </div>
      ) : (
        <>
          <Evidence r={claims} ciNote={ciNote} />
          {/* The gate's OWN countdown line, verbatim — the same sentence the CLI
              report and the masthead render. Re-deriving "3/20 high, 2/20 low"
              here would be a second definition of the countdown. */}
          <div className="sb-countdown mono">{claims.calibration.countdown}</div>
        </>
      )}
      <ScopeToggle
        r={r}
        disabledSet={disabledSet}
        stale={claims === null}
        onWrote={onWrote}
        onOutcome={onOutcome}
      />
    </div>
  )
}

export function StrategyBoard({ wake }: { wake: number }) {
  // A local bump refetches THIS panel the instant a write lands; the server's
  // action nonce (bumped by set_scope) wakes every other window over SSE.
  const [bump, setBump] = useState(0)
  // OUTSIDE the data swap, deliberately: the row that produced this result is
  // re-rendered (and its button re-labelled) by the very poll that follows the
  // write, so an outcome living inside the row would vanish with it.
  const [outcome, setOutcome] = useState<ScopeOutcome | null>(null)
  const st = usePolling(getStrategies, POLL_MS, wake + bump)
  const onWrote = () => setBump((b) => b + 1)

  // The same split GuardrailsPanel makes: `live` is force-nulled on any fetch
  // error and drives every truth CLAIM; `last` is the retained snapshot and
  // drives the CONTROLS, which therefore survive a failed poll. Acting on a
  // stale snapshot is safe because the SERVER is the authority: set_scope is an
  // unconditional idempotent whole-set write that can only ever subtract from
  // the env ceiling.
  const live = st.error === null ? st.data : null
  const last = st.data

  // Members of the stored subtraction that are not rows here (a play type
  // retired from the vocabulary while still switched off). Surfaced rather than
  // dropped: they ride every toggle's whole-set POST, which the repo tolerates
  // on re-save precisely so a stale member cannot lock the board out.
  const strays =
    last === null
      ? []
      : last.disabled.filter(
          (pt) => !last.strategies.some((r) => r.play_type === pt),
        )

  return (
    <section className="panel">
      <div className="panel-head">
        <HelpTerm term="execution scope (ceiling)">STRATEGY SCOPE</HelpTerm>
        <span className="panel-caption">
          selection is evidence-gated: the ceiling changes via env ceremony; the
          board can only subtract or restore within it.
        </span>
      </div>
      <div className="sb-body">
        {live !== null ? (
          <ScopeHeader s={live} />
        ) : (
          <DataUnknown error={st.error} noun="scope" />
        )}

        {/* OUTSIDE the read swap — see the split above. */}
        {outcome !== null && (
          <ScopeResult outcome={outcome} onDismiss={() => setOutcome(null)} />
        )}

        {last !== null && (
          <>
            <div className="gr-sub">
              RANKED BY <HelpTerm term="evidence tier">EVIDENCE</HelpTerm>
              <span className="gr-sub-note">
                tier first, then the CI floor — the gate&apos;s own number, never the
                point estimate · ungraded ranks last
              </span>
            </div>
            <div className="sb-rows">
              {last.strategies.map((r) => (
                <BoardRow
                  key={r.play_type}
                  r={r}
                  claims={live === null ? null : r}
                  ciNote={last.ci_note}
                  disabledSet={last.disabled}
                  onWrote={onWrote}
                  onOutcome={setOutcome}
                />
              ))}
            </div>
            {strays.length > 0 && (
              <div className="gr-note-row sb-strays">
                the board&apos;s subtraction also carries {strays.join(', ')} — not a
                current play type, so it subtracts nothing. It rides every save
                (dropping it here would re-enable something nobody asked to
                re-enable).
              </div>
            )}
          </>
        )}
      </div>
    </section>
  )
}
