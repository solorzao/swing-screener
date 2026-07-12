import { useEffect, useRef, useState } from 'react'
import type { Ref } from 'react'
import {
  ApiError,
  POLL_MS,
  getPlaybooks,
  getProposals,
  postApproveProposal,
  postWithdrawProposal,
  usePolling,
} from '../lib/api'
import type {
  ConcretePlayType,
  DriftReport,
  PlaybookBook,
  ProposalApproved,
  ProposalDecided,
  ProposalRow,
  VerdictRow,
} from '../lib/api'
import { dashOr, fmtR } from '../lib/fmt'
import { DeltaDiff } from '../components/DeltaDiff'
import { HoldToConfirm } from '../components/HoldToConfirm'
import { Markdown } from '../components/Markdown'
import { PanelBody } from '../components/PanelBody'
import { TierChip } from '../components/TierChip'

/* Screen 5 — Playbooks (plan Task 18): the honesty-critical verdict/provenance
   surface plus the two proposal WRITE actions (approve / withdraw).

   Every NUMBER on this screen comes from the code-owned SIDECAR
   (edge/<pt>.verdicts.json), never parsed out of the prose — the md is served
   verbatim and rendered as markdown, its numbers driving nothing. The honesty
   posture, top to bottom:
   - TierChip: forward_confirmed green (the app's first saturated green — zero
     exist today), replay_screened amber, hunch gray. UNKNOWN never green.
   - a verdict statistic NEVER renders a bare expectancy: n rides with it, and
     n=0 is "empty on this corpus", not a fabricated zero-edge.
   - provenance (cost_level / corpus_id) shows as-is; null renders "unstamped" /
     "—" (the row predates provenance stamping) — never hidden or faked.
   - drift is an ADVISORY structural check: null is UNKNOWN (dashed, never green),
     ok is calm-neutral (NOT green — green stays the tier edge claim), missing is
     amber with the missing tokens in its tooltip.
   - md_error → the book renders "prose unreadable (ClassName)", never blank or
     fabricated; verdicts_error → per-book honest degradation; store_errors → a
     loud banner.
   - APPROVE MARKS a proposal for promotion — it NEVER promotes. Promotion is
     three human edits in one commit (the approve response's checklist). Every
     decision is an uncommitted working-tree edit in edge/. */

/* ---------------- Verdicts ---------------- */

/** One verdict's expectancy cell — never a bare number: n rides with it, and
 * n=0 is the honest "empty on this corpus", not a zero-edge. */
function VerdictExpectancy({ v }: { v: VerdictRow }) {
  if (v.n === 0) {
    return <span className="pb-empty">empty on this corpus</span>
  }
  return (
    <span className="mono pb-exp">
      {dashOr(v.expectancy_r, fmtR)}
      <sup className="pb-n">
        {' '}
        n={v.n.toLocaleString('en-US')}·{v.n_clusters}c
      </sup>
    </span>
  )
}

/** The Bonferroni-corrected one-sided lower bound (the ci_note states it once);
 * dashed for an empty row or a non-finite bound. */
function VerdictBound({ v }: { v: VerdictRow }) {
  if (v.n === 0 || v.ci_low === null) return <span className="pb-dash">—</span>
  return (
    <span className="mono" title="Bonferroni-corrected one-sided lower bound">
      ≥ {fmtR(v.ci_low)}
    </span>
  )
}

/** cost_level / corpus_id straight off the wire — null is the honest "unstamped"
 * (the row predates provenance stamping), never backfilled. */
function Provenance({ v }: { v: VerdictRow }) {
  return (
    <span className="pb-prov mono">
      cost{' '}
      {v.cost_level === null ? (
        <em className="pb-unstamped" title="cost level not stamped at source — an honest unknown, never a default">
          unstamped
        </em>
      ) : (
        v.cost_level
      )}
      {' · '}corpus{' '}
      {v.corpus_id === null ? (
        <span className="pb-dash" title="corpus id not stamped at source">
          —
        </span>
      ) : (
        v.corpus_id
      )}
    </span>
  )
}

function VerdictTable({ verdicts }: { verdicts: VerdictRow[] }) {
  return (
    <div className="pb-table-wrap">
      <table className="pb-table">
        <thead>
          <tr>
            <th>tier</th>
            <th>bucket</th>
            <th>expectancy</th>
            <th>bound</th>
            <th>book</th>
            <th>provenance</th>
          </tr>
        </thead>
        <tbody>
          {verdicts.map((v) => (
            <tr key={`${v.dimension}=${v.bucket}`}>
              <td>
                <TierChip tier={v.tier} />
              </td>
              <td className="mono pb-bucket">
                {v.dimension}={v.bucket}
              </td>
              <td>
                <VerdictExpectancy v={v} />
              </td>
              <td>
                <VerdictBound v={v} />
              </td>
              <td className="mono pb-source">{v.source}</td>
              <td>
                <Provenance v={v} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ---------------- Drift ---------------- */

/** The advisory md-vs-sidecar structural check. null = UNKNOWN (dashed, never
 * green); ok = calm neutral (NOT green — green stays the tier edge claim);
 * missing = amber with the offending tokens in the tooltip. */
function DriftBadge({ drift }: { drift: DriftReport | null }) {
  if (drift === null) {
    return (
      <span
        className="pb-drift pb-drift-unknown"
        title="drift unknown — the prose or the sidecar could not be read; faithfulness against nothing is unknowable, never assumed ok"
      >
        <span className="pb-drift-dot" aria-hidden="true" />
        drift unknown
      </span>
    )
  }
  if (drift.ok) {
    return (
      <span
        className="pb-drift pb-drift-ok"
        title="structural check: every graded bucket appears in the matching prose section (advisory — hand-edits are legitimate)"
      >
        <span className="pb-drift-dot" aria-hidden="true" />
        md faithful
      </span>
    )
  }
  const tokens = drift.missing
    .map((m) => `${m.token} (expected in the ${m.tier} section)`)
    .join('; ')
  return (
    <span
      className="pb-drift pb-drift-warn"
      role="status"
      title={`possible drift — ${tokens}`}
    >
      <span className="pb-drift-dot" aria-hidden="true" />
      possible drift · {drift.missing.length} token{drift.missing.length === 1 ? '' : 's'}
    </span>
  )
}

/* ---------------- One book ---------------- */

function VerdictError({ error, pt }: { error: string; pt: string }) {
  if (error === 'missing') {
    return (
      <div className="panel-wait">
        no verdicts sidecar yet — {pt} has not been reflected (a setup state)
      </div>
    )
  }
  return (
    <div className="pb-verr" role="alert">
      verdicts sidecar unreadable — {error}. The prose still renders below, but there
      are no graded numbers to stand on.
    </div>
  )
}

function BookPanel({ book, ciNote }: { book: PlaybookBook; ciNote: string }) {
  const hasVerdicts = book.verdicts_error === null && book.verdicts.length > 0
  return (
    <section className="panel">
      <div className="panel-head">
        {book.play_type.toUpperCase()} PLAYBOOK
        {book.reflection_due && (
          <span
            className="pb-due"
            title="this play type's forward book has re-armed a reflection — the nightly job will regrade it"
          >
            REFLECTION DUE
          </span>
        )}
        <span className="spacer" />
        <DriftBadge drift={book.drift} />
      </div>

      <div className="pb-book">
        <div className="pb-fm mono">
          last reflected {book.frontmatter.last_reflected ?? 'never'} · forward closes
          at last reflection {book.frontmatter.forward_closed_at_last_reflection}
        </div>

        {book.verdicts_error !== null ? (
          <VerdictError error={book.verdicts_error} pt={book.play_type} />
        ) : book.verdicts.length === 0 ? (
          <div className="panel-wait">no graded buckets in the sidecar yet</div>
        ) : (
          <VerdictTable verdicts={book.verdicts} />
        )}
        {hasVerdicts && <div className="pb-cinote">{ciNote}</div>}

        <div className="pb-md-wrap">
          {book.md_error !== null ? (
            <div className="pb-md-error" role="alert">
              playbook prose unreadable — {book.md_error}. The numbers above come from
              the sidecar and stand on their own.
            </div>
          ) : book.md.trim() === '' ? (
            <div className="panel-wait">no playbook prose yet</div>
          ) : (
            <Markdown md={book.md} />
          )}
        </div>
      </div>
    </section>
  )
}

/* ---------------- Proposals (the two write actions) ---------------- */

function StatusChip({ status }: { status: string }) {
  if (status === 'queued') {
    return (
      <span className="pp-status pp-status-queued" title="queued — awaiting your decision">
        queued · decide
      </span>
    )
  }
  if (status === 'approved') {
    return (
      <span
        className="pp-status pp-status-approved"
        title="MARKED for promotion — a human still has to promote it (3-file commit); withdrawable until then"
      >
        approved · pending promotion
      </span>
    )
  }
  if (status === 'withdrawn') {
    return (
      <span className="pp-status pp-status-withdrawn" title="withdrawn — final until a human hand-edits the store">
        withdrawn
      </span>
    )
  }
  return <span className="pp-status pp-status-other">{status}</span>
}

type DecidePhase =
  | { kind: 'idle' }
  | { kind: 'firing'; action: 'approve' | 'withdraw' }
  | { kind: 'approved'; result: ProposalApproved }
  | { kind: 'withdrawn'; result: ProposalDecided }
  | { kind: 'error'; detail: string; status: number | null }

/** APPROVE marks (the response's checklist is the human's promotion path);
 * WITHDRAW just closes the idea. The wire `note` already says "uncommitted
 * working-tree edit — commit with your decision" — rendered verbatim. */
function DecisionResult({
  phase,
  onDismiss,
  dismissRef,
}: {
  phase: Extract<DecidePhase, { kind: 'approved' | 'withdrawn' | 'error' }>
  onDismiss: () => void
  /** Focus lands here when the panel mounts — the hold button the keyboard user
   * pressed was destroyed by the phase switch (mirrors CloseTradeForm's doneRef). */
  dismissRef: Ref<HTMLButtonElement>
}) {
  if (phase.kind === 'error') {
    const s = phase.status
    const head =
      s === 409
        ? 'DECISION CONFLICT'
        : s === 404
          ? 'PROPOSAL NOT FOUND'
          : s === 503
            ? 'STORE UNREADABLE'
            : 'DECISION FAILED'
    const sub =
      s === 409
        ? 'the store changed under you (already decided, or hand-edited) — reload to see its current state'
        : s === 503
          ? 'the proposal store is corrupt — fix it by hand; nothing was written'
          : s === null
            ? 'the write may or may not have landed — reload to check the current status'
            : null
    return (
      <div className="pp-result pp-result-error" role="alert">
        <div className="pp-result-head">{head}</div>
        <div className="pp-result-detail">{phase.detail}</div>
        {sub !== null && <div className="pp-result-note">{sub}</div>}
        <button type="button" className="pp-dismiss" ref={dismissRef} onClick={onDismiss}>
          dismiss
        </button>
      </div>
    )
  }
  if (phase.kind === 'approved') {
    const r = phase.result
    return (
      <div className="pp-result pp-result-approved" role="status">
        <div className="pp-result-head">MARKED FOR PROMOTION — {r.name} is now {r.status}</div>
        <div className="pp-result-note">
          This MARKED the proposal; it did NOT promote it. Promotion is three human
          edits, landed as one commit:
        </div>
        <ol className="pp-checklist">
          {r.checklist.map((step, idx) => (
            <li key={idx}>{step}</li>
          ))}
        </ol>
        <div className="pp-result-file mono">
          {r.note} · {r.file}
        </div>
        <button type="button" className="pp-dismiss" ref={dismissRef} onClick={onDismiss}>
          dismiss
        </button>
      </div>
    )
  }
  const r = phase.result
  return (
    <div className="pp-result pp-result-withdrawn" role="status">
      <div className="pp-result-head">WITHDRAWN — {r.name} is now {r.status}</div>
      <div className="pp-result-file mono">
        {r.note} · {r.file}
      </div>
      <button type="button" className="pp-dismiss" onClick={onDismiss}>
        dismiss
      </button>
    </div>
  )
}

/** The decision controls: a REQUIRED reason (the POST body needs it — the hold
 * trigger stays disabled until it is non-empty) plus a light 400ms
 * HoldToConfirm per legal transition. Approve is legal from `queued`; withdraw
 * from `queued` and `approved` (mirrors decide_proposal). A successful decision
 * local-bumps the proposals poll (scope decision 12; the server nonce wakes the
 * other windows). */
function ProposalDecider({
  p,
  onDecided,
}: {
  p: ProposalRow
  onDecided: () => void
}) {
  const [reason, setReason] = useState('')
  const [phase, setPhase] = useState<DecidePhase>({ kind: 'idle' })

  // Focus handoff (mirrors CloseTradeForm / DisarmControl): a fire destroys the
  // hold button, so land focus on the result's dismiss control; on dismiss,
  // return focus to a still-live hold trigger (approve if present, else
  // withdraw — a success may have removed the one that fired).
  const dismissRef = useRef<HTMLButtonElement>(null)
  const approveRef = useRef<HTMLButtonElement>(null)
  const withdrawRef = useRef<HTMLButtonElement>(null)
  const reasonRef = useRef<HTMLInputElement>(null)
  const focusBackRef = useRef(false)

  const pt = p.play_type as ConcretePlayType
  const canApprove = p.status === 'queued'
  const canWithdraw = p.status === 'queued' || p.status === 'approved'
  const reasonEmpty = reason.trim() === ''
  const busy = phase.kind === 'firing'
  const terminal =
    phase.kind === 'approved' || phase.kind === 'withdrawn' || phase.kind === 'error'

  useEffect(() => {
    if (terminal) {
      dismissRef.current?.focus()
    } else if (focusBackRef.current) {
      focusBackRef.current = false
      // Prefer an ENABLED hold trigger; after a success the reason is cleared
      // (disabling the triggers), so fall back to the reason input — the next
      // actionable control — and to nothing when the row is fully decided.
      const live = (b: HTMLButtonElement | null) => (b !== null && !b.disabled ? b : null)
      ;(live(approveRef.current) ?? live(withdrawRef.current) ?? reasonRef.current)?.focus()
    }
  }, [terminal, phase.kind])

  const dismiss = () => {
    focusBackRef.current = true
    setPhase({ kind: 'idle' })
  }

  const fire = (action: 'approve' | 'withdraw') => {
    setPhase({ kind: 'firing', action })
    const call =
      action === 'approve'
        ? postApproveProposal(pt, p.name, reason)
        : postWithdrawProposal(pt, p.name, reason)
    call.then(
      (result) => {
        if (action === 'approve') {
          setPhase({ kind: 'approved', result: result as ProposalApproved })
        } else {
          setPhase({ kind: 'withdrawn', result: result as ProposalDecided })
        }
        // Clear the reason on SUCCESS only — a following decision (approve then
        // withdraw) must not silently reuse it; an ERROR keeps it for a retry.
        setReason('')
        onDecided() // local key-bump refetch; the server nonce wakes other windows
      },
      (err: unknown) => {
        if (err instanceof ApiError) {
          setPhase({ kind: 'error', detail: err.message, status: err.status })
        } else {
          setPhase({
            kind: 'error',
            detail: 'backend unreachable — the decision may or may not have been written',
            status: null,
          })
        }
      },
    )
  }

  if (phase.kind === 'approved' || phase.kind === 'withdrawn' || phase.kind === 'error') {
    return <DecisionResult phase={phase} onDismiss={dismiss} dismissRef={dismissRef} />
  }

  if (!canApprove && !canWithdraw) {
    return (
      <div className="pp-decided">
        {p.status} — no further decision here (a human hand-edit reopens the store)
      </div>
    )
  }

  const reasonId = `pp-reason-${pt}-${p.name}`
  return (
    <div className="pp-decide">
      <label className="pp-reason-lab" htmlFor={reasonId}>
        decision reason <span className="pp-req">required</span>
      </label>
      <input
        id={reasonId}
        ref={reasonRef}
        className="pp-reason"
        value={reason}
        maxLength={200}
        placeholder="why — lands verbatim in the store's audit trail"
        onChange={(e) => setReason(e.target.value)}
      />
      <div className="pp-holds">
        {canApprove && (
          <HoldToConfirm
            holdMs={400}
            className="pp-approve"
            disabled={reasonEmpty || busy}
            buttonRef={approveRef}
            title={
              reasonEmpty
                ? 'enter a reason first'
                : 'hold 400 ms — MARKS this proposal for promotion (it does not promote; promotion is your 3-file commit)'
            }
            label={<span className="pp-hold-label">hold to MARK for promotion</span>}
            onFire={() => fire('approve')}
          />
        )}
        {canWithdraw && (
          <HoldToConfirm
            holdMs={400}
            className="pp-withdraw"
            disabled={reasonEmpty || busy}
            buttonRef={withdrawRef}
            title={
              reasonEmpty
                ? 'enter a reason first'
                : 'hold 400 ms — withdraws this proposal (an uncommitted working-tree edit)'
            }
            label={<span className="pp-hold-label">hold to withdraw</span>}
            onFire={() => fire('withdraw')}
          />
        )}
      </div>
      <div className="pp-decide-note">
        approving MARKS — promotion stays a manual 3-file commit (shown on approve) ·
        every decision edits edge/ in your working tree, uncommitted
      </div>
    </div>
  )
}

function ProposalCard({ p, onDecided }: { p: ProposalRow; onDecided: () => void }) {
  const gateOk = p.gate_verdict === 'ok'
  return (
    <div className="pp">
      <div className="pp-headrow">
        <span className="pp-name mono">{p.name}</span>
        <StatusChip status={p.status} />
        <span className="pp-pt">{p.play_type}</span>
        <span className="spacer" />
        {p.noop && (
          <span
            className="pp-noop"
            title="a valid delta that EQUALS the incumbent config — it cannot change the book (the optimizer would skip it)"
          >
            no-op
          </span>
        )}
      </div>
      <div className="pp-meta mono">
        drafted {p.drafted_at} · {p.provenance} · hunch {p.hunch_ref}
      </div>
      <DeltaDiff rows={p.delta_vs_incumbent} />
      <div
        className={gateOk ? 'pp-gate pp-gate-ok' : 'pp-gate pp-gate-warn'}
        title={
          gateOk
            ? 'passes the config gatekeeper (a valid delta)'
            : 'the config gatekeeper REFUSES this delta — it is invalid, not merely a no-op'
        }
      >
        gate: {gateOk ? 'ok' : p.gate_verdict}
      </div>
      <div className="pp-rationale">{p.rationale}</div>
      <ProposalDecider p={p} onDecided={onDecided} />
    </div>
  )
}

/* ---------------- Screen ---------------- */

export function PlaybooksScreen({ wake }: { wake: number }) {
  // Both polls are per-screen — they mount here and die with the screen. A
  // decision local-bumps `bump`, which re-params the proposals poll for an
  // immediate refetch; the server action nonce wakes everything else via SSE.
  const [bump, setBump] = useState(0)
  const playbooks = usePolling(getPlaybooks, POLL_MS, wake)
  const proposals = usePolling(getProposals, POLL_MS, wake + bump)
  const onDecided = () => setBump((b) => b + 1)

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          PLAYBOOKS
          <span className="panel-caption">
            numbers come from the code-owned sidecar · the prose is verbatim · the
            first saturated green is a forward-confirmed verdict (none exist yet)
          </span>
          <span className="spacer" />
          {playbooks.data !== null && (
            <span className="pb-duesum">
              reflection due:{' '}
              {playbooks.data.due_play_types.length === 0
                ? 'none'
                : playbooks.data.due_play_types.join(', ')}
            </span>
          )}
        </div>
        <PanelBody polled={playbooks} noun="playbooks">
          {(data) => (
            <div className="pb-books">
              {data.store_errors.length > 0 && (
                <div className="pb-banner" role="alert">
                  verdicts unreadable for: {data.store_errors.join(', ')} — those books
                  render without graded numbers (fix the sidecar by hand)
                </div>
              )}
              {data.books.map((book) => (
                <BookPanel key={book.play_type} book={book} ciNote={data.ci_note} />
              ))}
            </div>
          )}
        </PanelBody>
      </section>

      <section className="panel">
        <div className="panel-head">
          PROPOSED VARIANTS
          <span className="panel-caption">
            approving MARKS for promotion — it never promotes; every decision is an
            uncommitted edit in edge/
          </span>
        </div>
        <PanelBody polled={proposals} noun="proposals">
          {(data) => (
            <div className="pp-list">
              {data.store_errors.length > 0 && (
                <div className="pb-banner" role="alert">
                  proposal store unreadable for: {data.store_errors.join(', ')} — fix it
                  by hand
                </div>
              )}
              {data.proposals.length === 0 && data.store_errors.length === 0 ? (
                <div className="panel-wait">
                  no proposals — the reflection drafts them from durable hunches
                </div>
              ) : (
                data.proposals.map((p) => (
                  <ProposalCard
                    key={`${p.play_type}:${p.name}`}
                    p={p}
                    onDecided={onDecided}
                  />
                ))
              )}
            </div>
          )}
        </PanelBody>
      </section>
    </main>
  )
}
