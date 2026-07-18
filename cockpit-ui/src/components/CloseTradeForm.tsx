import { useEffect, useRef, useState } from 'react'
import { ApiError, postCloseTrade } from '../lib/api'
import type { CloseTradeResult, RealPositionRow } from '../lib/api'
import { dashOr, fmtR, fmtSignedUsd, localTodayIso } from '../lib/fmt'

/* CLOSE TRADE — the second action. Closes one open REAL trade at the price
   Oliver reports; the server writes the trade close and its
   ExitEvent(reason='manual_close') in one transaction and answers with the
   realized R and $.

   Wire-truth rules: the exit price prefills from the row's `last_close`
   (null → the field stays empty, labeled "no quote"); the R/$ preview is
   client math labeled as a preview; the SUCCESS panel renders the wire's
   `realized_r`/`realized_usd` — a null realized_r means the recorded risk is
   degenerate (stop at/above entry) and renders "not computable", NEVER 0.

   409 (already closed) is a STATE, not a failure: the form resolves with a
   'conflict' outcome — the caller refetches the list and dismisses this form
   (the row is gone from the open set; re-showing a dead form would invite a
   second close of a closed trade). A bare network failure renders the honest
   unknown: the close may or may not have landed.

   Focus flow mirrors DisarmControl's precedent (Task 15): completing the close
   disables/replaces the button that had focus, so a keyboard user would be
   dropped to <body> at the exact moment they need to read the result — instead
   focus lands on the result's "done" button (a post-commit effect, never a
   pre-commit focus that no-ops against a not-yet-rendered node). Returning
   focus to the row on dismiss is the PARENT's job (the CLOSE button lives in
   the table, not here). */

/** Exit-reason vocabulary: the engine's ExitReason literals
 * (signals/exits.py) plus the manual default — the wire accepts any ≤32-char
 * string, but a select keeps the ledger's reason facet queryable. */
const REASONS = ['manual', 'stop', 'target', 'momentum_flip', 'time_stop'] as const

type Phase =
  | { kind: 'editing' }
  | { kind: 'submitting' }
  | { kind: 'closed'; result: CloseTradeResult }

export function CloseTradeForm({
  row,
  onClosed,
  onConflict,
  onDismiss,
  onBusyChange,
}: {
  row: RealPositionRow
  /** The close LANDED — bump the refetch now (the result panel stays up; it
   * must live OUTSIDE the open table so the vanishing row can't unmount it). */
  onClosed: () => void
  /** 409 already-closed — refetch the list and dismiss this form. */
  onConflict: (detail: string) => void
  /** Cancel, or "done" after reading the result. */
  onDismiss: () => void
  /** True while a close POST is in flight — the parent disables the row CLOSE
   * buttons so a second trade can't be opened mid-submit (the 409-race guard's
   * belt-and-suspenders companion). */
  onBusyChange: (busy: boolean) => void
}) {
  // Rounded to cents for the input: an editable prefill off the raw wire
  // float, not a record — the user reports the actual fill anyway.
  const [exitPrice, setExitPrice] = useState(
    row.last_close === null ? '' : row.last_close.toFixed(2),
  )
  const [exitDate, setExitDate] = useState(localTodayIso())
  const [reason, setReason] = useState<string>('manual')
  const [phase, setPhase] = useState<Phase>({ kind: 'editing' })
  const [error, setError] = useState<string | null>(null)
  const doneRef = useRef<HTMLButtonElement>(null)

  // Post-commit focus handoff: once the result panel has rendered, land focus
  // on its "done" button so the keyboard user reads the outcome (the button
  // they held was destroyed by the phase switch).
  useEffect(() => {
    if (phase.kind === 'closed') doneRef.current?.focus()
  }, [phase.kind])

  const priceNum = exitPrice.trim() === '' ? null : Number(exitPrice)
  const parsable = priceNum !== null && Number.isFinite(priceNum)

  // Client preview of what the wire will report — labeled, never the record.
  const risk = row.entry_price - row.stop
  const previewR = parsable && risk > 0 ? (priceNum - row.entry_price) / risk : null
  const previewUsd = parsable ? (priceNum - row.entry_price) * row.size : null

  const submitting = phase.kind === 'submitting'

  const submit = () => {
    setPhase({ kind: 'submitting' })
    setError(null)
    onBusyChange(true)
    postCloseTrade(row.trade_id, {
      // null for unparseable input — the server's 422 names the problem.
      exit_price: (parsable ? priceNum : null) as number,
      exit_date: exitDate,
      exit_reason: reason,
    }).then(
      (result) => {
        onBusyChange(false)
        setPhase({ kind: 'closed', result })
        onClosed() // local key-bump refetch; the SSE nonce covers other windows
      },
      (err: unknown) => {
        onBusyChange(false)
        if (err instanceof ApiError && err.status === 409) {
          // Already closed: a state, not a failure — refetch + dismiss.
          onConflict(err.message)
          return
        }
        setPhase({ kind: 'editing' })
        setError(
          err instanceof ApiError
            ? err.message
            : 'backend unreachable — the close may or may not have been ' +
              'recorded; refresh before retrying',
        )
      },
    )
  }

  if (phase.kind === 'closed') {
    const r = phase.result
    return (
      <div className="ctf" role="status">
        <div className="ctf-done-head">
          CLOSED — trade #{r.trade_id} · {row.ticker} · {r.exit_date} ·{' '}
          {r.exit_reason}
        </div>
        <div className="ctf-done-nums mono">
          realized{' '}
          {r.realized_r === null ? (
            <span
              className="ctf-nocompute"
              title="the recorded risk is degenerate (stop at/above entry) — R has no denominator"
            >
              R not computable
            </span>
          ) : (
            fmtR(r.realized_r)
          )}{' '}
          · {fmtSignedUsd(r.realized_usd)}
        </div>
        <button type="button" className="ctf-btn" ref={doneRef} onClick={onDismiss}>
          done
        </button>
      </div>
    )
  }

  return (
    <div className="ctf">
      <div className="ctf-head">
        CLOSE trade #{row.trade_id} · {row.ticker} · entry ${row.entry_price.toFixed(2)}{' '}
        × {row.size}
      </div>
      <div className="ctf-fields">
        <label className="ctf-field">
          <span className="ctf-lab">
            exit price{' '}
            {row.last_close === null ? (
              <em className="ctf-noquote">(no quote — enter it)</em>
            ) : (
              <em className="ctf-noquote">(prefilled from last close)</em>
            )}
          </span>
          <input
            className="ctf-in mono"
            type="number"
            step="0.01"
            value={exitPrice}
            aria-invalid={error !== null || undefined}
            aria-describedby={error !== null ? 'ctf-err' : undefined}
            onChange={(e) => setExitPrice(e.target.value)}
          />
        </label>
        <label className="ctf-field">
          <span className="ctf-lab">exit date</span>
          <input
            className="ctf-in mono"
            type="date"
            value={exitDate}
            onChange={(e) => setExitDate(e.target.value)}
          />
        </label>
        <label className="ctf-field">
          <span className="ctf-lab">reason</span>
          <select
            className="ctf-in mono"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          >
            {REASONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="ctf-preview mono">
        {parsable ? (
          <>
            preview:{' '}
            {previewR === null ? (
              <span
                className="ctf-nocompute"
                title="recorded risk is degenerate (stop at/above entry)"
              >
                R not computable
              </span>
            ) : (
              fmtR(previewR)
            )}{' '}
            · {dashOr(previewUsd, fmtSignedUsd)}
            <span className="ctf-preview-note">
              {' '}
              — the wire's numbers land on close
            </span>
          </>
        ) : (
          <span className="ctf-preview-note">enter an exit price to preview R/$</span>
        )}
      </div>

      {error !== null && (
        <div className="ctf-err" id="ctf-err" role="alert">
          {error}
        </div>
      )}

      <div className="ctf-actions">
        <button
          type="button"
          className="ctf-btn ctf-submit"
          onClick={submit}
          disabled={submitting}
        >
          {submitting ? 'closing…' : 'CLOSE TRADE'}
        </button>
        <button
          type="button"
          className="ctf-btn"
          onClick={onDismiss}
          disabled={submitting}
        >
          cancel
        </button>
      </div>
    </div>
  )
}
