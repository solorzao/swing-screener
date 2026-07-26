/* Shared display formatters for the cockpit screens (Task 16 onward). ONE
   spelling of every money / R / percent / clock cell so the trade screens —
   and Task 17's PickCard + LevelRail — never diverge into subtly different
   negative signs or decimal counts.

   Negative numbers render with a TRUE minus sign (U+2212 '−', never the ASCII
   hyphen '-') placed BEFORE the currency symbol: −$12.34, not $-12.34. Every
   nullable cell routes through `dashOr` — a not-measured / absent value renders
   an em dash, never a fabricated 0 (the honesty rule this whole screen turns
   on). */

const MINUS = '−' // − : the typographic minus, wider than a hyphen

/** Plain dollars — no leading '+' on positives, grouped thousands, `decimals`
 * places (0 for whole-dollar risk cells). `$315.32`, `−$52.50`, `$1,000`. */
export function fmtUsd(v: number, decimals = 2): string {
  const abs = Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
  return `${v < 0 ? MINUS : ''}$${abs}`
}

/** Signed dollars — ALWAYS a leading sign (P/L, realized). `+$154.00`, `−$52.50`. */
export function fmtSignedUsd(v: number): string {
  const abs = Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return `${v < 0 ? MINUS : '+'}$${abs}`
}

/** Signed R multiple. `+0.67R`, `−0.27R`. */
export function fmtR(v: number): string {
  return `${v < 0 ? MINUS : '+'}${Math.abs(v).toFixed(2)}R`
}

/** A FRACTION on the wire (0.042, not 4.2) → a signed percent. `+4.2%`, `−1.5%`. */
export function fmtPct(v: number): string {
  return `${v < 0 ? MINUS : '+'}${Math.abs(v * 100).toFixed(1)}%`
}

/** A FRACTION on the wire (0.55) → a whole-number, UNSIGNED percent: a count
 * tally (win rate, fill rate), not a signed change. `55%`, `100%`, `0%`. Distinct
 * from `fmtPct`, which signs a 1-decimal fractional delta. */
export function fmtCountPct(v: number): string {
  return `${Math.round(v * 100)}%`
}

/** Share / size count — whole when integral, else two decimals. */
export function fmtSize(v: number): string {
  return v % 1 === 0 ? v.toFixed(0) : v.toFixed(2)
}

/** A 24-hour wall-clock time from an ISO timestamp (the "as of" stamp); the raw
 * string back unchanged if it does not parse. */
export function fmtClock(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleTimeString('en-US', { hour12: false })
}

/** An ISO timestamp as a LOCAL day + 24-hour clock — "2026-07-25 14:03:21". For
 * history lists that span days; `fmtClock`'s time-only form stays the same-session
 * "as of" stamp. The raw string back unchanged if it does not parse. */
export function fmtStamp(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const clock = d.toLocaleTimeString('en-US', { hour12: false })
  return `${d.getFullYear()}-${mm}-${dd} ${clock}`
}

/** Today in the LOCAL calendar as YYYY-MM-DD — the day date-scoped forms and
 * filters default to. LOCAL deliberately: the cockpit runs beside its server,
 * so the client's local day matches the server-naive day the journals filter
 * on. Consolidates three identical per-screen copies (Journal notebook /
 * GEX lab / CloseTradeForm). */
export function localTodayIso(): string {
  const d = new Date()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** The null gate: `fmt(v)` for a real number, an em dash for null (not-measured
 * / absent) — never a fabricated 0. Every nullable money/R/percent cell on the
 * trade screens routes through here. */
export function dashOr(v: number | null, fmt: (n: number) => string): string {
  return v === null ? '—' : fmt(v)
}

/** A Stat's point value with its unit suffix — sign always shown, 2–3 decimals
 * (2 at magnitude ≥ 10), e.g. "+0.057R". The negative-zero guard: a tiny
 * negative like -0.0004 rounds to all-zero digits and must render "+0.000R",
 * never "-0.000R" — a minus on a zero reads as a real loss. Reproduces
 * StatChip's original formatter VERBATIM (the ASCII minus the chip has always
 * shown) so StatChip and the ProvenancePopover share ONE spelling with no
 * import cycle — a pure move, not a restyle. */
export function fmtStatValue(value: number, unit: string): string {
  const decimals = Math.abs(value) >= 10 ? 2 : 3
  let fixed = value.toFixed(decimals)
  if (Number(fixed) === 0) fixed = (0).toFixed(decimals) // strips the "-" of "-0.000"
  const sign = fixed.startsWith('-') ? '' : '+'
  return `${sign}${fixed}${unit}`
}

/** The backend's raw cost-level string as the chip glyph: "0.05" → "@05",
 * "0.10" → "@10"; anything not matching `0.xx` is prefixed with "@" verbatim. */
export function costGlyph(costLevel: string): string {
  return `@${costLevel.replace(/^0\./, '')}`
}
