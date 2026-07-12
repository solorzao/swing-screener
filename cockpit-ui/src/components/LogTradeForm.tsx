import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, getTradeDefaults, postLogTrade } from '../lib/api'
import type { FieldError, LogTradeResult, TradeDefaults } from '../lib/api'
import { fmtUsd } from '../lib/fmt'

/* LOG TRADE — the first of the six actions (design rule 3: structured forms
   with ENGINE-SUPPLIED defaults). Records a trade Oliver actually took; places
   no order.

   Two paths through the form:
   - PREFILLED: a signal id loads GET /api/trade-defaults — the engine's levels
     VERBATIM, live actionability, the conviction-'medium' share count — and the
     submit carries `signal_id`, so the server verifies the fill against the
     plan and stamps `override`. While editing, a client-side preview mirrors
     the server's `_override_note` math; it is labeled a preview because the
     SERVER recomputes authoritatively at insert (routers/trades.py).
   - MANUAL: no signal id — the row lands `unlinked (manual)`, no override.

   Honesty rules carried here: the success panel renders the WIRE response
   (trade id, entry_date, the stamped override note verbatim) — never what the
   form submitted; `shares == 0` renders "sizing unconfigured", never a guessed
   size; Pydantic 422 field errors render inline on the named field, the
   model-level rows (loc '') and hand-raised details (e.g. "unknown signal_id")
   on the form line; a bare network failure (no ApiError .status) says the
   backend is unreachable AND that the outcome is unknown — the POST may have
   landed. */

/* --- The client-side mirror of routers/trades.py override stamping --- */

/** Prices within this absolute tolerance count as EQUAL (mirrors
 * `_FAITHFUL_TOL` — the prefill round-trips through JSON floats and a number
 * input, so exact equality would preview phantom "moved 0.0%" overrides). */
const FAITHFUL_TOL = 0.005

function pctPart(label: string, actual: number, planned: number): string | null {
  if (planned <= 0 || Math.abs(actual - planned) <= FAITHFUL_TOL) return null
  const pct = ((actual - planned) / planned) * 100
  const text = `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`
  if (text === '+0.0%' || text === '-0.0%') return null
  return `${label} moved ${text}`
}

/** The preview of what the server will stamp into `override`, or null when
 * faithful — same format, same tolerances, same zone-R unit as
 * `_override_note`. Advisory only: the server's stamp is the record. */
function overridePreview(
  d: TradeDefaults,
  entry: number,
  stop: number,
  target: number,
): string | null {
  const parts: string[] = []
  const risk = d.signal.entry_ceiling - d.signal.stop
  if (risk > FAITHFUL_TOL) {
    if (entry > d.signal.entry_ceiling + FAITHFUL_TOL) {
      parts.push(
        `entry +${((entry - d.signal.entry_ceiling) / risk).toFixed(2)}R above ceiling`,
      )
    } else if (entry < d.signal.entry_floor - FAITHFUL_TOL) {
      parts.push(
        `entry -${((d.signal.entry_floor - entry) / risk).toFixed(2)}R below floor`,
      )
    }
  }
  for (const part of [
    pctPart('stop', stop, d.signal.stop),
    pctPart('target', target, d.signal.target),
  ]) {
    if (part !== null) parts.push(part)
  }
  return parts.length === 0 ? null : parts.join('; ').slice(0, 256)
}

/* --- Form state --- */

interface Fields {
  ticker: string
  timeframe: string
  horizon: string
  entry_price: string
  size: string
  stop: string
  target: string
  notes: string
}

const EMPTY: Fields = {
  ticker: '',
  timeframe: '1d',
  horizon: 'medium',
  entry_price: '',
  size: '',
  stop: '',
  target: '',
  notes: '',
}

/** Parse a number input the honest way: '' / garbage becomes null and the
 * SERVER's validator names the problem (422 inline) — no client-side guess. */
const num = (s: string): number | null => {
  if (s.trim() === '') return null
  const v = Number(s)
  return Number.isFinite(v) ? v : null
}

type Phase =
  | { kind: 'editing' }
  | { kind: 'submitting' }
  | { kind: 'logged'; result: LogTradeResult; withSignal: boolean }

/** The field names the server knows — the aria wiring keys and the "stray
 * error" complement both derive from HERE, so a renamed field can never drift
 * out of sync (loc '' is the model-level bucket; handled separately). */
const KNOWN_FIELDS: readonly string[] = [...Object.keys(EMPTY), 'signal_id']

export function LogTradeForm({
  onLogged,
  initialSignalId,
}: {
  onLogged: () => void
  /** When set (a pick's LOG action), the form mounts already loading that
   * signal's engine defaults — the same path the manual "load" button takes.
   * The caller keys the form on this id so a new pick remounts + reloads. */
  initialSignalId?: number
}) {
  const [fields, setFields] = useState<Fields>(EMPTY)
  const [signalIdInput, setSignalIdInput] = useState('')
  const [defaults, setDefaults] = useState<TradeDefaults | null>(null)
  const [defaultsSignalId, setDefaultsSignalId] = useState<number | null>(null)
  const [defaultsError, setDefaultsError] = useState<string | null>(null)
  const [loadingDefaults, setLoadingDefaults] = useState(false)
  const [phase, setPhase] = useState<Phase>({ kind: 'editing' })
  const [formError, setFormError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<FieldError[]>([])

  const set = (k: keyof Fields) => (v: string) =>
    setFields((f) => ({ ...f, [k]: v }))

  const errFor = (name: string): string | null => {
    const hit = fieldErrors.find((fe) => fe.loc === name)
    return hit === undefined ? null : hit.msg
  }
  /** Field errors whose loc names no rendered input — they surface on their
   * own line so no server message is ever silently dropped. loc '' (the
   * model-level rows, e.g. stop-vs-entry geometry) is excluded: those already
   * ride `formError`, and doubling them here would render twice. The known set
   * derives from the fields themselves (KNOWN_FIELDS) — never a hand-kept list. */
  const strayErrors = fieldErrors.filter(
    (fe) => fe.loc !== '' && !KNOWN_FIELDS.includes(fe.loc),
  )

  /** aria wiring for one field's input: mark it invalid and point it at its
   * error node so a submit-time 422 is announced, not silent (fix pattern for
   * the three form surfaces that copy this). Accepts signal_id too — it lives
   * in the prefill row, not `Fields`, but carries its own 422. */
  const fieldAria = (name: keyof Fields | 'signal_id') =>
    errFor(name) !== null
      ? ({ 'aria-invalid': true, 'aria-describedby': `ltferr-${name}` } as const)
      : {}

  const loadDefaults = () => {
    const id = num(signalIdInput)
    if (id === null) {
      setDefaultsError('enter a numeric signal id')
      return
    }
    loadDefaultsFor(id)
  }

  const loadDefaultsFor = (id: number) => {
    setLoadingDefaults(true)
    setDefaultsError(null)
    getTradeDefaults(id).then(
      (d) => {
        setLoadingDefaults(false)
        setDefaults(d)
        setDefaultsSignalId(id)
        setFields((f) => ({
          ...f,
          ticker: d.signal.ticker,
          timeframe: d.signal.timeframe,
          horizon: d.signal.horizon,
          // suggested_entry is the cached close CLAMPED into the zone; null
          // without a quote — the field stays empty rather than guessing.
          // Rounded to cents for the input: an editable prefill, not a record,
          // and cents sit far inside the server's 0.005 faithfulness tolerance.
          entry_price:
            d.suggested_entry === null ? '' : d.suggested_entry.toFixed(2),
          stop: String(d.signal.stop),
          target: String(d.signal.target),
          // shares == 0 is "sizing unconfigured" — never prefill a fake size.
          size: d.sizing.unconfigured ? '' : String(d.sizing.shares),
        }))
      },
      (err: unknown) => {
        setLoadingDefaults(false)
        setDefaultsError(
          err instanceof ApiError
            ? err.message
            : 'backend unreachable — defaults not loaded',
        )
      },
    )
  }

  const clearPrefill = () => {
    setDefaults(null)
    setDefaultsSignalId(null)
    setSignalIdInput('')
    setDefaultsError(null)
  }

  // Prefill-on-mount: a pick's LOG action mounts this form (keyed on the id, so
  // a different pick remounts) with initialSignalId set — auto-load its engine
  // defaults exactly as the manual "load" button would. Mount-only: the id is
  // fixed for this instance's lifetime by the key.
  useEffect(() => {
    if (initialSignalId !== undefined) {
      setSignalIdInput(String(initialSignalId))
      loadDefaultsFor(initialSignalId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only; the id is pinned by the caller's key
  }, [])

  const submit = () => {
    setPhase({ kind: 'submitting' })
    setFormError(null)
    setFieldErrors([])
    const body = {
      ticker: fields.ticker,
      timeframe: fields.timeframe,
      horizon: fields.horizon,
      // null for unparseable input — the server's 422 names the field.
      entry_price: num(fields.entry_price) as number,
      size: num(fields.size) as number,
      stop: num(fields.stop) as number,
      target: num(fields.target) as number,
      notes: fields.notes,
      ...(defaultsSignalId !== null ? { signal_id: defaultsSignalId } : {}),
    }
    postLogTrade(body).then(
      (result) => {
        setPhase({ kind: 'logged', result, withSignal: defaultsSignalId !== null })
        setFields(EMPTY)
        clearPrefill()
        onLogged() // local key-bump refetch (scope decision 12); SSE covers other windows
      },
      (err: unknown) => {
        setPhase({ kind: 'editing' })
        if (err instanceof ApiError) {
          if (err.fieldErrors !== null) {
            setFieldErrors(err.fieldErrors)
            const unfielded = err.fieldErrors.filter((fe) => fe.loc === '')
            if (unfielded.length > 0)
              setFormError(unfielded.map((fe) => fe.msg).join('; '))
          } else {
            // hand-raised string detail: "unknown signal_id", 503s, 403.
            setFormError(err.message)
          }
        } else {
          setFormError(
            'backend unreachable — the trade may or may not have been logged; ' +
              'if it was, it will appear in the open table',
          )
        }
      },
    )
  }

  // The live preview needs every price parsed; unparseable fields = no claim.
  const pEntry = num(fields.entry_price)
  const pStop = num(fields.stop)
  const pTarget = num(fields.target)
  const preview =
    defaults !== null && pEntry !== null && pStop !== null && pTarget !== null
      ? overridePreview(defaults, pEntry, pStop, pTarget)
      : null

  // Stale-prefill honesty: the signal-id INPUT can be edited after a load (or
  // a failed re-load leaves the old defaults up), so the loaded id and the box
  // can diverge. `defaultsSignalId` is what the submit actually stamps against
  // (never the box), so the panel names it and flags the divergence — the
  // prefill/preview are for the LOADED id, and a re-load is one click away.
  const divergent =
    defaults !== null &&
    defaultsSignalId !== null &&
    num(signalIdInput) !== defaultsSignalId

  return (
    <div className="ltf">
      <div className="ltf-prefill">
        <label className="ltf-lab" htmlFor="ltf-signal-id">
          signal id
        </label>
        <input
          id="ltf-signal-id"
          className="ltf-in ltf-in-id mono"
          type="number"
          value={signalIdInput}
          {...fieldAria('signal_id')}
          onChange={(e) => setSignalIdInput(e.target.value)}
          placeholder="optional"
        />
        <button
          type="button"
          className="ltf-btn"
          onClick={loadDefaults}
          disabled={loadingDefaults}
        >
          {loadingDefaults ? 'loading…' : 'load engine defaults'}
        </button>
        {defaults !== null && (
          <button type="button" className="ltf-btn ltf-btn-quiet" onClick={clearPrefill}>
            clear prefill
          </button>
        )}
        {defaultsError !== null && (
          <span className="ltf-err" role="alert">
            {defaultsError}
          </span>
        )}
      </div>

      {defaults !== null && (
        <div className={divergent ? 'ltf-defaults ltf-diverged' : 'ltf-defaults'}>
          {divergent && (
            <div className="ltf-diverge-flag" role="status">
              prefill is for signal #{defaultsSignalId}; the box now says{' '}
              {signalIdInput.trim() === '' ? '(blank)' : `#${signalIdInput.trim()}`} —
              re-load to prefill (and stamp against) that one, or the submit uses
              #{defaultsSignalId}
            </div>
          )}
          <div className="ltf-def-row mono">
            signal #{defaultsSignalId} · {defaults.signal.ticker} ·{' '}
            {defaults.signal.play_type} · {defaults.signal.timeframe}/
            {defaults.signal.horizon} · conviction {defaults.signal.conviction_tier}
          </div>
          <div className="ltf-def-row mono">
            zone {fmtUsd(defaults.signal.entry_floor)}–
            {fmtUsd(defaults.signal.entry_ceiling)} · stop{' '}
            {fmtUsd(defaults.signal.stop)} · target {fmtUsd(defaults.signal.target)}
          </div>
          <div className="ltf-def-row">
            <span className="mono">
              last close{' '}
              {defaults.last_close === null ? '—' : fmtUsd(defaults.last_close)}
            </span>
            <span className={`ltf-act ltf-act-${defaults.actionability.status}`}>
              {defaults.actionability.status}
              {defaults.actionability.dist_r !== null &&
                ` · ${defaults.actionability.dist_r >= 0 ? '+' : ''}${defaults.actionability.dist_r.toFixed(2)}R past entry`}
            </span>
          </div>
          <div className="ltf-def-row">
            {defaults.sizing.unconfigured ? (
              <span className="ltf-unconf">
                sizing unconfigured — no engine share count; enter size yourself
              </span>
            ) : (
              <span className="mono">
                engine size {defaults.sizing.shares} shares · risk{' '}
                {fmtUsd(defaults.sizing.risk_dollars)}
              </span>
            )}
          </div>
        </div>
      )}

      <div className="ltf-grid">
        <Field name="ticker" label="ticker" error={errFor('ticker')}>
          <input
            className="ltf-in mono"
            value={fields.ticker}
            {...fieldAria('ticker')}
            onChange={(e) => set('ticker')(e.target.value)}
          />
        </Field>
        <Field name="timeframe" label="timeframe" error={errFor('timeframe')}>
          <input
            className="ltf-in mono"
            value={fields.timeframe}
            {...fieldAria('timeframe')}
            onChange={(e) => set('timeframe')(e.target.value)}
          />
        </Field>
        <Field name="horizon" label="horizon" error={errFor('horizon')}>
          <input
            className="ltf-in mono"
            value={fields.horizon}
            {...fieldAria('horizon')}
            onChange={(e) => set('horizon')(e.target.value)}
          />
        </Field>
        <Field name="entry_price" label="entry price" error={errFor('entry_price')}>
          <input
            className="ltf-in mono"
            type="number"
            step="0.01"
            value={fields.entry_price}
            {...fieldAria('entry_price')}
            onChange={(e) => set('entry_price')(e.target.value)}
          />
        </Field>
        <Field name="size" label="size (shares)" error={errFor('size')}>
          <input
            className="ltf-in mono"
            type="number"
            step="1"
            value={fields.size}
            {...fieldAria('size')}
            onChange={(e) => set('size')(e.target.value)}
          />
        </Field>
        <Field name="stop" label="stop" error={errFor('stop')}>
          <input
            className="ltf-in mono"
            type="number"
            step="0.01"
            value={fields.stop}
            {...fieldAria('stop')}
            onChange={(e) => set('stop')(e.target.value)}
          />
        </Field>
        <Field name="target" label="target" error={errFor('target')}>
          <input
            className="ltf-in mono"
            type="number"
            step="0.01"
            value={fields.target}
            {...fieldAria('target')}
            onChange={(e) => set('target')(e.target.value)}
          />
        </Field>
        <Field name="notes" label="notes" error={errFor('notes')} wide>
          <input
            className="ltf-in"
            value={fields.notes}
            {...fieldAria('notes')}
            onChange={(e) => set('notes')(e.target.value)}
          />
        </Field>
      </div>

      {defaults !== null && (
        <div className="ltf-preview">
          {preview === null ? (
            <span className="ltf-faithful">
              engine-faithful so far — no override would be stamped
            </span>
          ) : (
            <span className="ltf-override">override preview: {preview}</span>
          )}
          <span className="ltf-preview-note">
            {' '}
            — preview only; the server recomputes authoritatively at insert
          </span>
        </div>
      )}

      {errFor('signal_id') !== null && (
        <div className="ltf-err" id="ltferr-signal_id" role="alert">
          signal_id: {errFor('signal_id')}
        </div>
      )}
      {strayErrors.length > 0 && (
        <div className="ltf-err" role="alert">
          {strayErrors.map((fe) => `${fe.loc}: ${fe.msg}`).join('; ')}
        </div>
      )}
      {formError !== null && (
        <div className="ltf-err" role="alert">
          {formError}
        </div>
      )}

      <div className="ltf-actions">
        <button
          type="button"
          className="ltf-btn ltf-submit"
          onClick={submit}
          disabled={phase.kind === 'submitting'}
        >
          {phase.kind === 'submitting' ? 'logging…' : 'LOG TRADE'}
        </button>
        <span className="ltf-note">records it — places no order · entry date is stamped server-side (today)</span>
      </div>

      {phase.kind === 'logged' && (
        <div className="ltf-done" role="status">
          <div className="ltf-done-head">
            LOGGED — trade #{phase.result.trade_id} · entry date{' '}
            {phase.result.entry_date}
          </div>
          {/* The WIRE's stamp, verbatim — never the client preview. null means
              the server stamped nothing: engine-faithful when a signal rode
              the POST, unlinked (manual) when none did. */}
          {phase.result.override !== null ? (
            <div className="ltf-override">override: {phase.result.override}</div>
          ) : (
            <div className="ltf-faithful">
              no override stamped
              {phase.withSignal ? ' — engine-faithful' : ' — manual, unlinked to a signal'}
            </div>
          )}
          <button
            type="button"
            className="ltf-btn ltf-btn-quiet"
            onClick={() => setPhase({ kind: 'editing' })}
          >
            dismiss
          </button>
        </div>
      )}
    </div>
  )
}

function Field({
  name,
  label,
  error,
  wide,
  children,
}: {
  /** The wire field name — the error node's id (`ltferr-<name>`) matches the
   * input's aria-describedby, so a 422 on this field is announced. */
  name: string
  label: string
  error: string | null
  wide?: boolean
  children: ReactNode
}) {
  return (
    <label className={wide === true ? 'ltf-field ltf-wide' : 'ltf-field'}>
      <span className="ltf-lab">{label}</span>
      {children}
      {error !== null && (
        <span className="ltf-field-err" id={`ltferr-${name}`} role="alert">
          {error}
        </span>
      )}
    </label>
  )
}
