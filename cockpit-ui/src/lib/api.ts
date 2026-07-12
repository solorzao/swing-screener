/* Typed mirror of the cockpit API contract (src/swing_screener/cockpit/api.py +
   cockpit/routers/*.py). Every statistic arrives as a full Stat — there is
   deliberately NO helper here that returns or formats a bare number (design
   rule 1). The Phase-3 types transcribe the routers' hand-rolled response dicts
   FAITHFULLY, degradation markers included (md_error, verdicts_error,
   store_errors, the total actionability shape, null-means-not-measured) — a
   field invented here would be a lie about the wire. */

import { useEffect, useRef, useState } from 'react'

/* Poll budget: forward-books + performance each cost ~1s server-side, so 60s is
   the floor for EVERY poll; the SSE wake (useEventWake) makes changes feel
   instant without touching that budget. Screens import this — one budget, one
   spelling. */
export const POLL_MS = 60_000

/** A statistic with full provenance — the wire form of cockpit/stats.py `Stat`. */
export interface Stat {
  value: number
  n: number
  n_clusters: number
  ci_low: number
  ci_high: number
  cost_level: string | null
  corpus_id: string | null
  facet: string
  unit: string
  thin_clusters: boolean
}

export interface CohortRow {
  key: string
  strength: string | null
  stat: Stat
}

export interface Cohorts {
  cohorts: CohortRow[]
}

export interface Health {
  connected: boolean
  label: string
  error: string | null
  /** True iff the DB URL is Azure — gates the sign-in affordance. */
  azure: boolean
}

export type HeartbeatState = 'up' | 'late' | 'down' | 'unknown'

export interface Heartbeat {
  name: string
  state: HeartbeatState
  /** ISO-8601 timestamp of the last run, or null when the job has never run. */
  last: string | null
  period_s: number
  grace_s: number
  detail: string
}

/** The two ways of counting: `research` (would_surface, the wide grid) vs `gold`
 * (what production actually surfaced). Mirrors the backend `facet` query param. */
export type Facet = 'research' | 'gold'

/** Trailing leaderboard window in days ('all' = no cut). Shadows the DOM global —
 * always `import type { Window }` and name locals `win`, never `window`. */
export type Window = 'all' | '90' | '180' | '365'

export type PlayType = 'all' | 'continuation' | 'reversal'

/** Lifecycle of a registered experiment book — wire form of settlement.py STATES. */
export type SettlementState =
  | 'accruing'
  | 'settled-awaiting-decision'
  | 'futile-awaiting-decision'
  | 'retired'

/** One Forward Books wall card — the wire form of settlement.py `SettlementCard`. */
export interface SettlementCard {
  name: string
  kind: 'arm' | 'variant'
  play_type: string
  state: SettlementState
  n_accrued: number
  /** Closes needed to settle, by CI-shrinkage projection. null means "cannot
   * project yet" (n < 5 or a collapsed CI) and is deliberately never 0, which
   * would read as "done" — the renderer must not conflate null (no read) with
   * small (nearly done). */
  n_needed: number | null
  /** Projected settlement date (ISO), from the trailing-30d close rate; null
   * when unprojectable (n_needed null) or accrual stalled. */
  eta: string | null
  stopping_rule: string
  registered_sha: string
  /** ISO-8601 date the experiment was registered. */
  registered_at: string
  mde_r: number
  book: Stat
  control: Stat
  delta: Stat
  /** How the delta's CI was built — 'clustered' (variants) | 'iid' (arms). */
  upper_bound_type: 'clustered' | 'iid'
  /** Trailing-expectancy spark points as (ISO date, R) pairs — drift, not cumulative R. */
  spark: [string, number][]
  decision: string | null
}

/** Cards arrive pre-ordered: awaiting-decision → accruing → retired. */
export interface ForwardBooks {
  cards: SettlementCard[]
}

/** The latest daily digest's reversal funnel snapshot. */
export interface Funnel {
  run_date: string
  detected: number
  confirmed: number
  fresh: number
  actionable: number
  surfaced: number
  overflow: string[]
  pool_n: number
  confirmed_only: boolean
  premium_only: boolean
  /** A live-quote fn was injected at digest time — NOT "the check ran":
   * _drop_already_ran fails open on a quote outage, so true can coexist with
   * actionable == fresh on an outage day. */
  already_ran_checked: boolean
}

/** `funnel` is null before the first digest records a snapshot — a normal state. */
export interface FunnelResponse {
  funnel: Funnel | null
}

export interface PerformanceKpis {
  expectancy: Stat
  win_rate: number
  fill_rate: number
  /** null when the book has no losers — JSON has no Infinity (the page's ∞ glyph). */
  profit_factor: number | null
  n_closed: number
}

export interface LeaderboardRow {
  variant: string
  stat: Stat
  win_rate: number
  fill_rate: number
  n_total: number
  /** Trust label, worst-first precedence: iid beats thin beats ok. */
  flag: 'iid' | 'thin' | 'ok'
}

export interface ArmRow {
  arm: string
  stat: Stat
  n_pairs: number
  /** Paired delta vs baseline; null on the baseline row (no self-delta). */
  delta: Stat | null
}

export interface BreakdownRow {
  key: string
  stat: Stat
  win_rate: number
  n_closed: number
}

/** The Streamlit Screener Performance page as data — every aggregate a Stat.
 * Degenerate sections arrive as empty lists, never omitted keys: rendering
 * decisions belong to the frontend. */
export interface Performance {
  kpis: PerformanceKpis
  leaderboard: LeaderboardRow[]
  arms: ArmRow[]
  breakdowns: {
    timeframe: BreakdownRow[]
    rank: BreakdownRow[]
    score: BreakdownRow[]
    market_trend: BreakdownRow[]
    market_vol: BreakdownRow[]
  }
  /** Cumulative-R equity curve as (ISO date, R) pairs. */
  equity_curve: [string, number][]
}

/** The advisory autonomy gate + today's analyst spend, as one status object. */
export interface Gate {
  ready: boolean
  /** Multi-line preformatted text (format pinned by test_autonomy_countdown.py). */
  countdown: string
  execution_mode: string
  /** Settings TRUTHINESS (is SWING_BROKER set), NEVER connectivity — the
   * masthead DISARM enablement keys on it; it rides this already-polled
   * endpoint so the always-visible masthead needs no extra poll. */
  broker_configured: boolean
  analyst_spend_today_usd: number
}

/* ---------- Phase 3 wire shapes (routers/trades.py) ---------- */

/** POST /api/trades body — the server validates (422 with field detail):
 * ticker required (stripped+uppered), entry/size > 0, stop < entry < target. */
export interface TradeCreate {
  ticker: string
  /** Defaults server-side: timeframe '1d', horizon 'medium', notes ''. */
  timeframe?: string
  horizon?: string
  entry_price: number
  size: number
  stop: number
  target: number
  notes?: string
  /** Set when the form was prefilled from a pick — the server verifies the row
   * exists (422 otherwise) and stamps `override` from the plan deviation. */
  signal_id?: number | null
}

export interface LogTradeResult {
  trade_id: number
  /** Server-computed deviation summary, rendered VERBATIM ("entry +0.50R above
   * ceiling; stop moved +1.1%"). null = faithful OR unprefilled — the unlinked
   * tag (signal_id null) tells those apart, not this. */
  override: string | null
  entry_date: string
}

/** POST /api/trades/{id}/close body. exit_date (ISO) defaults to today
 * server-side; a blank reason falls back to 'manual'. */
export interface TradeClose {
  exit_price: number
  exit_date?: string
  exit_reason?: string
}

export interface CloseTradeResult {
  trade_id: number
  /** null when the recorded risk is degenerate (stop raised to/above entry) —
   * the close itself still happened. */
  realized_r: number | null
  realized_usd: number
  exit_date: string
  exit_reason: string
}

/** The TOTAL actionability shape every surface serves: a missing quote or a
 * degenerate zone reads status 'unknown' with dist_r null — never a null
 * object, so the frontend has ONE shape everywhere. */
export interface Actionability {
  status: 'actionable' | 'extended' | 'broken' | 'unknown'
  dist_r: number | null
}

/** Bracket lamp states (cockpit/common._bracket): no broker snapshot →
 * 'unknown' (absence of evidence is never a claim); a real/manual row's honest
 * ceiling is 'db-only'; 'unprotected' is a wire-contract state, unreachable
 * today (both stop columns are NOT NULL). */
export type BracketState = 'unknown' | 'armed' | 'db-only' | 'unprotected'

/** Per-row P/L block. Real rows serve every field; live rows null-guard per
 * FIELD (no shares join → dollar P/L null while the R-multiple still renders).
 * `unrealized_pct` is a FRACTION on the wire (0.04, not 4.0). */
export interface PositionPl {
  unrealized_pl: number | null
  unrealized_pct: number | null
  r_multiple: number | null
  dist_to_stop_pct: number | null
  dist_to_target_pct: number | null
}

/** 'red' price ≤ stop, 'yellow' price ≥ target, 'green' between, 'unknown'
 * without a price. Computed even on a row whose P/L math is broken — the
 * get-out lamp never degrades with the arithmetic. */
export type PositionBadge = 'red' | 'yellow' | 'green' | 'unknown'

/** One REAL (manually logged) open-trade row. `pl: null` = the row's P/L math
 * is untrustable (per-row degradation — the row is KEPT). */
export interface RealPositionRow {
  kind: 'real'
  trade_id: number
  ticker: string
  timeframe: string
  entry_price: number
  size: number
  stop: number
  target: number
  /** The close form's prefill source; null on a quote miss. */
  last_close: number | null
  pl: PositionPl | null
  badge: PositionBadge
  bracket: BracketState
  override: string | null
  signal_id: number | null
  unlinked: boolean
}

/** One LIVE (broker-owned) open-position row. entry_price null = pending entry
 * (resting limit unfilled); size is the ExecutionLog shares join or null. */
export interface LivePositionRow {
  kind: 'live'
  paper_id: number
  ticker: string
  timeframe: string
  entry_price: number | null
  size: number | null
  stop: number
  target: number
  last_close: number | null
  pl: PositionPl | null
  badge: PositionBadge
  bracket: BracketState
  /** Live rows have no override column — always null on the wire. */
  override: null
  signal_id: number | null
  unlinked: boolean
}

export type PositionRow = RealPositionRow | LivePositionRow

/** One cap gauge. `limit: null` is UNBOUNDED (no cap set) — never 0/0. */
export interface CapUsage {
  used: number
  limit: number | null
}

export interface PositionCaps {
  account: string
  /** null with no runs yet — the day-scoped `used` values are honest zeros. */
  run_date: string | null
  notional: CapUsage
  /** An R THRESHOLD: today's realized R with sign preserved (the breaker fires
   * at used <= -limit) — NOT a spent-dollars meter. Label it as R. */
  loss_r: CapUsage
  concurrent: CapUsage
}

export interface ClosedTrade {
  trade_id: number
  ticker: string
  entry_date: string
  exit_date: string | null
  entry_price: number
  exit_price: number | null
  size: number
  realized_usd: number
  exit_reason: string | null
}

export interface Positions {
  open: PositionRow[]
  caps: PositionCaps
  /** Newest exit first (the repo's order). */
  closed: ClosedTrade[]
  /** Realized-equity points as (ISO exit date, running USD) pairs; an undated
   * close is listed in `closed` but never plotted. */
  equity: [string, number][]
  quotes_as_of: string
  broker_as_of: string | null
}

/** GET /api/trade-defaults — the log-trade form's prefill: the engine's levels
 * VERBATIM, live actionability, and the conviction-'medium' size. */
export interface TradeDefaults {
  signal: {
    ticker: string
    timeframe: string
    horizon: string
    play_type: string
    entry_floor: number
    entry_ceiling: number
    stop: number
    target: number
    conviction_tier: string
  }
  last_close: number | null
  actionability: Actionability
  /** The cached close CLAMPED into [floor, ceiling]; null without a quote. */
  suggested_entry: number | null
  /** shares == 0 is the deliberate "sizing unconfigured" signal — render
   * R-multiples, never a guessed dollar. */
  sizing: { shares: number; risk_dollars: number; unconfigured: boolean }
}

/* ---------- Phase 3 wire shapes (routers/analysis.py) ---------- */

export type AnalysisStatus = 'queued' | 'running' | 'done' | 'failed'

export interface AnalysisCreated {
  id: number
  ticker: string
  status: AnalysisStatus
  requested_at: string
}

export interface AnalysisRequestRow {
  id: number
  ticker: string
  status: AnalysisStatus
  /** Running longer than the worker's requeue window — the next worker pass
   * will requeue it, so say "stalled — will retry", never spin. */
  stalled: boolean
  requested_at: string | null
  started_at: string | null
  finished_at: string | null
  summary: string
  /** Whitelist-gated server-side; legacy raw messages read "error (details in log)". */
  error: string | null
  has_pdf: boolean
  chart_count: number
}

export interface AnalysisList {
  requests: AnalysisRequestRow[]
  // Who drains the queue: "cloud (*/15min)" for an Azure DB, else "manual" —
  // manual means requests wait for `python -m swing_screener.notify.ondemand`.
  worker: string
}

/* ---------- Phase 3 wire shapes (routers/proposals.py) ---------- */

/** The two concrete play types (store/decision path params) — the query-side
 * `PlayType` adds 'all' on top for the performance filters. */
export type ConcretePlayType = 'continuation' | 'reversal'

/** One knob's scalar value in a proposal delta (JSON-native by store contract). */
export type KnobValue = number | string | boolean

export interface ProposalDeltaRow {
  knob: string
  /** The incumbent StrategyConfig value; null when the knob names no real field. */
  current: KnobValue | null
  proposed: KnobValue
}

export interface ProposalRow {
  name: string
  play_type: string
  delta: Record<string, KnobValue>
  rationale: string
  hunch_ref: string
  /** 'queued' | 'approved' | 'withdrawn' (plus whatever a hand-edit wrote). */
  status: string
  drafted_at: string
  provenance: string
  /** 'ok', or the gatekeeper's ValueError text verbatim (our own config prose). */
  gate_verdict: string
  delta_vs_incumbent: ProposalDeltaRow[]
  /** Mirrors the optimizer's no-op guard: a valid delta equal to the incumbent.
   * A row that FAILS the gate is invalid, not a no-op — noop stays false. */
  noop: boolean
}

export interface Proposals {
  proposals: ProposalRow[]
  /** Play types whose store was unreadable — names ONLY (leak posture). */
  store_errors: string[]
}

export interface ProposalDecided {
  name: string
  play_type: string
  status: string
  /** The store's repo-relative label (never a resolved path). */
  file: string
  /** The honesty line: an uncommitted working-tree edit — commit it yourself. */
  note: string
}

export interface ProposalApproved extends ProposalDecided {
  /** The verbatim three-artifact promotion checklist — approve MARKS, never promotes. */
  checklist: string[]
}

/* ---------- Phase 3 wire shapes (routers/safety.py) ---------- */

export interface DisarmResult {
  dry_run: boolean
  cancelled: { symbol: string; broker_order_id: string }[]
  sells_kept: number
  stops_restored: string[]
  /** Positions with no recorded stop level anywhere — loud, left alone. */
  unprotected: string[]
}

export interface PreflightCheck {
  name: string
  ok: boolean
  detail: string
  critical: boolean
}

export interface BracketShieldRow {
  symbol: string
  qty: number
  state: BracketState
}

export interface ExecutionSafety {
  /** Settings truthiness, never connectivity (same rule as /api/gate). */
  broker_configured: boolean
  mode: string
  /** The honesty label: everything here reads THIS process's env. */
  env_scope: string
  locks: { mode_is_live: boolean; allow_real_money: boolean; gate_ready: boolean }
  caps_mandate: { ok: boolean; reason: string }
  preflight: { go: boolean; checks: PreflightCheck[] }
  /** known: false (no broker / venue read failed) renders UNKNOWN, never green. */
  bracket_shield: { known: boolean; as_of: string | null; positions: BracketShieldRow[] }
}

/* ---------- Phase 3 wire shapes (routers/picks.py) ---------- */

/** Today's grade + that grade's scored record; n=0/mean null = "unproven". */
export interface PickAnalyst {
  grade: string
  n: number
  mean_r: number | null
}

export interface PickRow {
  signal_id: number
  ticker: string
  play_type: string
  timeframe: string
  horizon: string
  rank: number
  score: number
  strength: string | null
  conviction_tier: string
  entry_floor: number
  entry_ceiling: number
  stop: number
  target: number
  last_close: number | null
  actionability: Actionability
  is_repeat: boolean
  /** Chart-proxy 404s are NORMAL — most signals are chartless. */
  has_chart: boolean
  /** The client-side cohorts join key. */
  cohort: { play_type: string; strength: string | null }
  /** null when no AnalystCall exists for the pick — the chip is simply absent. */
  analyst: PickAnalyst | null
}

/** The digest's surfaced sets in digest ORDER, plus liveness-dropped picks as
 * flagged extras that never consumed a cap slot — the five always match the
 * email. `extended` is NORMAL for a reversal (a resting limit sits above its
 * ceiling by definition). run_date null = no screen run yet, a setup state. */
export interface Picks {
  run_date: string | null
  daily: PickRow[]
  reversal: PickRow[]
  extras: PickRow[]
  quotes_as_of: string
}

/* ---------- Phase 3 wire shapes (routers/reference.py) ---------- */

export type TickerSource = 'exit' | 'execution' | 'email' | 'analyst' | 'analysis'

/** One Zone E event. Exit rows ADDITIONALLY carry the exit-log facet axes
 * (account / is_paper / reason / tier); other sources omit them. */
export interface TickerEvent {
  source: TickerSource
  ts: string
  ticker: string | null
  headline: string
  detail: string
  account?: string
  is_paper?: boolean
  reason?: string
  tier?: string
}

export interface TickerFeed {
  events: TickerEvent[]
}

export interface ExitRow {
  id: number
  date: string
  trade_id: number | null
  is_paper: boolean
  account: string
  tier: string
  reason: string
  message: string
}

export interface Exits {
  exits: ExitRow[]
}

/** The exit log's three facets — Book (is_paper) and Account are DIFFERENT
 * axes: the research grid and the curated intent book are both is_paper=true. */
export interface ExitFilters {
  reason?: string
  book?: 'paper' | 'real'
  account?: string
  limit?: number
}

export interface UniverseRow {
  ticker: string
  name: string
  exchange: string
  market_cap: number | null
  avg_dollar_volume: number | null
  sector: string | null
}

export interface Universe {
  rows: UniverseRow[]
}

export interface EmailRow {
  id: number
  sent_at: string | null
  kind: string
  subject: string
  run_date: string | null
}

export interface Emails {
  emails: EmailRow[]
}

/* ---------- Phase 3 wire shapes (routers/playbooks.py) ---------- */

export type VerdictTier = 'forward_confirmed' | 'replay_screened' | 'hunch'

/** One sidecar verdict row — tier rides HERE, never on a Stat (closed key set).
 * ci_low is the Bonferroni-corrected one-sided lower bound (the response's
 * ci_note says so); verdicts carry NO ci_high. cost_level/corpus_id null =
 * the row predates provenance stamping — render "not measured", never a default. */
export interface VerdictRow {
  play_type: string
  dimension: string
  bucket: string
  tier: VerdictTier
  n: number
  expectancy_r: number | null
  ci_low: number | null
  n_clusters: number
  source: 'forward' | 'replay' | 'none'
  cost_level: string | null
  corpus_id: string | null
}

/** The structural md-vs-sidecar check — an ADVISORY amber, never numeric. */
export interface DriftReport {
  ok: boolean
  missing: { token: string; tier: string }[]
}

export interface PlaybookBook {
  play_type: string
  /** The edge file VERBATIM ("" when missing) — prose only; numbers on screen
   * come from `verdicts`. */
  md: string
  /** null = read fine (missing included); "unreadable (ClassName)" = the read
   * RAISED (the cp1252 hand-edit case). */
  md_error: string | null
  frontmatter: {
    forward_closed_at_last_reflection: number
    last_reflected: string | null
  }
  verdicts: VerdictRow[]
  /** null = read; 'missing' = never reflected (a setup state);
   * "unreadable (ClassName)" = corrupt (also lands in store_errors). */
  verdicts_error: string | null
  /** null = explicitly UNKNOWN (either input unreadable) — never a fabricated ok. */
  drift: DriftReport | null
  /** The "Falsified / retired" section body, pre-extracted for strikethrough. */
  falsified: string
  reflection_due: boolean
}

export interface Playbooks {
  books: PlaybookBook[]
  due_play_types: string[]
  store_errors: string[]
  /** The one wire statement of what a verdict's bound IS. */
  ci_note: string
}

/* ---------- Phase 3 wire shapes (routers/weather.py) ---------- */

export interface Weather {
  run_date: string
  ha_alignment: string
  flipped: boolean
  spy_vs_200dma: string | null
  vol_bucket: string | null
  vix: number | null
  vix_rank: number | null
  vix_spike: boolean
  ten_year: number | null
  three_month: number | null
  yield_inverted: boolean | null
  bond_trend: string | null
  vix_term_ratio: number | null
  vix_backwardation: boolean
  credit_chg_4w: number | null
  credit_pctile: number | null
  cyc_def_trend: string | null
  cyc_def_chg_4w: number | null
  breadth_trend: string | null
  breadth_chg_4w: number | null
  recession_prob: number | null
  /** false marks the deterministic FALLBACK report (no LLM ran) — badge it. */
  is_deep: boolean
  core: string
  report: string
  created_at: string | null
}

/** weather null = no report has ever run (weekly job; a fresh DB has none). */
export interface WeatherResponse {
  weather: Weather | null
  history: { run_date: string; ha_alignment: string; flipped: boolean; core: string }[]
}

/* ---------- Phase 3 wire shapes (routers/analyst.py) ---------- */

/** One conviction grade's scored record; n=0/mean null = "unproven", never omitted. */
export interface CalibrationRow {
  grade: string
  n: number
  mean_r: number | null
}

export interface AnalystFreshness {
  scored: number
  pending_in_window: number
  /** Judgments the shadow book never tested — the unfilled fraction's numerator. */
  expired_unfilled: number
}

/** The autonomy gate's calibration test. ci_low null = insufficient data (the
 * true value is -inf, which JSON cannot carry) — the lamp reads UNKNOWN, never green. */
export interface CalibrationProgress {
  calibrated: boolean
  high_minus_low: number
  ci_low: number | null
  n_high: number
  n_low: number
  n_clusters_high: number
  n_clusters_low: number
  reason: string
  min_per_bucket: number
  cluster_floor: number
}

export interface AnalystPlayType {
  play_type: string
  calibration: CalibrationRow[]
  /** Scored calls where the analyst MOVED the baseline; null when none scored. */
  nudge: { n: number; mean_r: number } | null
  freshness: AnalystFreshness
  progress: CalibrationProgress
}

export interface AnalystSpend {
  today_usd: number
  last_7d_usd: number
  last_30d_usd: number
  /** In-window calls with NO estimate — the sums can only UNDERcount. */
  uncosted_calls_30d: number
  note: string
}

/** Every R here is the SHADOW BOOK's (r_basis says so) — no real dollars. */
export interface AnalystReport {
  play_types: AnalystPlayType[]
  spend: AnalystSpend
  today: string
  r_basis: string
}

/** The Needs-Your-Hand strip's permanent-poll feed — cheap file+DB reads only.
 * Corrupt stores degrade QUIETLY here (the loud markers live on the screens). */
export interface Attention {
  proposals_queued: string[]
  proposals_approved_pending: string[]
  reflection_due: string[]
  /** Compare to the localStorage last-seen id for the unread badge (client-side). */
  latest_analysis_id: number | null
}

/** One 422 field error, flattened from FastAPI's Pydantic detail row: `loc` is
 * the dotted field path with the leading 'body' segment dropped ("stop",
 * "signal_id"), `msg` the validator's own message. */
export interface FieldError {
  loc: string
  msg: string
}

/** Every non-2xx response throws this. `message` is ALWAYS human-renderable —
 * the backend's one safe {detail} string (503 "database unreachable (...)",
 * 409 conflicts), or the joined field messages on a Pydantic 422 — so naive
 * `err.message` renderers (PanelBody's error line, usePolling) keep working
 * unchanged. `fieldErrors` carries the per-field rows only when the body had
 * the Pydantic LIST shape, for forms that want to mark individual inputs;
 * `status` lets an action surface branch (409 vs 422 vs 503) without string
 * matching. Additive over Error — instanceof Error still holds. */
export class ApiError extends Error {
  readonly status: number
  readonly fieldErrors: FieldError[] | null

  constructor(message: string, status: number, fieldErrors: FieldError[] | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

/** One Pydantic detail row → FieldError, defensively: rows are backend-shaped
 * ({loc: (string|number)[], msg, type}) but this never trusts that. Only the
 * LEADING 'body' segment is dropped (it names the request part, not a field) —
 * a deeper segment that happens to spell "body" is a real field name and stays. */
function toFieldError(row: unknown): FieldError {
  const r = (typeof row === 'object' && row !== null ? row : {}) as {
    loc?: unknown
    msg?: unknown
  }
  const rawLoc: unknown[] = Array.isArray(r.loc) ? r.loc : []
  const parts = rawLoc[0] === 'body' ? rawLoc.slice(1) : rawLoc
  const loc = parts.map(String).join('.')
  return { loc, msg: typeof r.msg === 'string' ? r.msg : JSON.stringify(row) }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    // Backend errors come in TWO shapes, both under {"detail": ...}: the
    // hand-raised endpoints put ONE safe string there, while Pydantic
    // validation (422) puts a LIST of {loc, msg, type} rows — TradeCreate's
    // field messages ride that shape and must reach the screen, not collapse
    // into a bare "HTTP 422". Anything else keeps the bare status line.
    let message = `HTTP ${res.status}`
    let fieldErrors: FieldError[] | null = null
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') {
        message = body.detail
      } else if (Array.isArray(body.detail) && body.detail.length > 0) {
        fieldErrors = body.detail.map(toFieldError)
        message = fieldErrors
          .map((fe) => (fe.loc === '' ? fe.msg : `${fe.loc}: ${fe.msg}`))
          .join('; ')
      }
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new ApiError(message, res.status, fieldErrors)
  }
  return (await res.json()) as T
}

export const getHealth = (): Promise<Health> => fetchJson<Health>('/api/health')

export const getHeartbeats = (): Promise<Heartbeat[]> =>
  fetchJson<Heartbeat[]>('/api/heartbeats')

export const getCohorts = (facet: Facet = 'research'): Promise<Cohorts> =>
  fetchJson<Cohorts>(`/api/stats/cohorts?facet=${facet}`)

export const getForwardBooks = (facet: Facet = 'research'): Promise<ForwardBooks> =>
  fetchJson<ForwardBooks>(`/api/forward-books?facet=${facet}`)

export const getFunnel = (): Promise<FunnelResponse> => fetchJson<FunnelResponse>('/api/funnel')

export const getPerformance = (
  playType: PlayType,
  win: Window, // `window` would shadow the global — `win` everywhere in here
  facet: Facet,
): Promise<Performance> =>
  fetchJson<Performance>(
    `/api/stats/performance?play_type=${playType}&window=${win}&facet=${facet}`,
  )

export const getGate = (): Promise<Gate> => fetchJson<Gate>('/api/gate')

/** POST an action with the X-Cockpit guard header (the one mutation guard — it
 * forces cross-origin callers into a failing CORS preflight; same-origin us
 * attaches it trivially). Backend errors surface their one safe {detail} line
 * via fetchJson. Every action fetcher routes here — an action POST without the
 * header is a 403 by construction. `signal` lets a caller cancel a request it
 * no longer wants (an aborted fetch rejects with an AbortError DOMException —
 * a caller that passes one owns that rejection). */
export const postAction = <T>(
  url: string,
  body?: object,
  signal?: AbortSignal,
): Promise<T> =>
  fetchJson<T>(url, {
    method: 'POST',
    headers:
      body === undefined
        ? { 'X-Cockpit': '1' }
        : { 'X-Cockpit': '1', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

export interface AzureLoginResponse {
  started: boolean
  already_running?: boolean
  error?: string
}

/** Kick off `az login` on the backend. The response only says whether a login
 * process STARTED — recovery is observed via /api/health, never via this call. */
export const postAzureLogin = (): Promise<AzureLoginResponse> =>
  postAction<AzureLoginResponse>('/api/azure-login')

/* ---------- Phase 3 fetchers ---------- */

/** Log a REAL trade Oliver actually took — records it, places no order. */
export const postLogTrade = (body: TradeCreate): Promise<LogTradeResult> =>
  postAction<LogTradeResult>('/api/trades', body)

/** Close a real trade at the reported price. 404 unknown id, 409 already closed. */
export const postCloseTrade = (
  tradeId: number,
  body: TradeClose,
): Promise<CloseTradeResult> =>
  postAction<CloseTradeResult>(`/api/trades/${tradeId}/close`, body)

export const getPositions = (): Promise<Positions> =>
  fetchJson<Positions>('/api/positions')

export const getTradeDefaults = (signalId: number): Promise<TradeDefaults> =>
  fetchJson<TradeDefaults>(`/api/trade-defaults?signal_id=${signalId}`)

/** Queue an on-demand deep-analysis run. The SERVER stamps requested_at (UTC). */
export const postAnalysis = (ticker: string): Promise<AnalysisCreated> =>
  postAction<AnalysisCreated>('/api/analysis', { ticker })

export const getAnalysisList = (limit = 50): Promise<AnalysisList> =>
  fetchJson<AnalysisList>(`/api/analysis?limit=${limit}`)

/* The three byte proxies are URL BUILDERS, not fetchers — they feed <img src>
   and <a href> directly. Resolution is server-side from DB-stored keys only;
   404 is a NORMAL state (aged-out blob, chartless signal). */
export const analysisChartUrl = (requestId: number, index: number): string =>
  `/api/analysis/${requestId}/chart/${index}`

export const analysisPdfUrl = (requestId: number): string =>
  `/api/analysis/${requestId}/pdf`

export const signalChartUrl = (signalId: number): string =>
  `/api/signals/${signalId}/chart`

export const getProposals = (): Promise<Proposals> =>
  fetchJson<Proposals>('/api/proposals')

/** Approve one QUEUED proposal — approve MARKS, never promotes; the response
 * carries the three-artifact promotion checklist (all human, one commit). */
export const postApproveProposal = (
  playType: ConcretePlayType,
  name: string,
  reason: string,
): Promise<ProposalApproved> =>
  postAction<ProposalApproved>(
    `/api/proposals/${playType}/${encodeURIComponent(name)}/approve`,
    { reason },
  )

/** Withdraw a proposal — legal from queued AND from approved. */
export const postWithdrawProposal = (
  playType: ConcretePlayType,
  name: string,
  reason: string,
): Promise<ProposalDecided> =>
  postAction<ProposalDecided>(
    `/api/proposals/${playType}/${encodeURIComponent(name)}/withdraw`,
    { reason },
  )

/** The DISARM runbook step: pull entry-side orders, keep the stops. dry_run
 * previews what a real run WOULD do — the venue is untouched. 409 = no broker
 * configured (a state, not a crash) or a disarm already in flight. `signal`
 * exists for the PREVIEW only: an aborted hold cancels its in-flight dry run
 * outright instead of orphaning it server-side. */
export const postDisarm = (
  dryRun: boolean,
  signal?: AbortSignal,
): Promise<DisarmResult> =>
  postAction<DisarmResult>(`/api/disarm?dry_run=${dryRun ? 1 : 0}`, undefined, signal)

export const getExecutionSafety = (): Promise<ExecutionSafety> =>
  fetchJson<ExecutionSafety>('/api/execution/safety')

export const getPicks = (): Promise<Picks> => fetchJson<Picks>('/api/picks')

export const getTicker = (limit = 50): Promise<TickerFeed> =>
  fetchJson<TickerFeed>(`/api/ticker?limit=${limit}`)

export const getExits = (filters: ExitFilters = {}): Promise<Exits> => {
  const params = new URLSearchParams()
  if (filters.reason !== undefined) params.set('reason', filters.reason)
  if (filters.book !== undefined) params.set('book', filters.book)
  if (filters.account !== undefined) params.set('account', filters.account)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  const qs = params.toString()
  return fetchJson<Exits>(qs === '' ? '/api/exits' : `/api/exits?${qs}`)
}

export const getUniverse = (search?: string): Promise<Universe> =>
  fetchJson<Universe>(
    search === undefined || search === ''
      ? '/api/universe'
      : `/api/universe?search=${encodeURIComponent(search)}`,
  )

export const getEmails = (limit = 100): Promise<Emails> =>
  fetchJson<Emails>(`/api/emails?limit=${limit}`)

export const getPlaybooks = (): Promise<Playbooks> =>
  fetchJson<Playbooks>('/api/playbooks')

export const getWeather = (): Promise<WeatherResponse> =>
  fetchJson<WeatherResponse>('/api/weather')

export const getAnalyst = (): Promise<AnalystReport> =>
  fetchJson<AnalystReport>('/api/analyst')

export const getAttention = (): Promise<Attention> =>
  fetchJson<Attention>('/api/attention')

export interface Polled<T> {
  data: T | null
  error: string | null
  /** Wall-clock time of the last SUCCESSFUL fetch (feeds the data-as-of clock). */
  lastFetched: Date | null
}

/** Poll `fetcher` every `ms` (first fetch immediately). Errors — 503s, network —
 * land in `error` and never crash the tree; the last good `data` is kept so the
 * shell stays alive while the per-panel error line shows.
 *
 * This hook is the polling TEMPLATE every future component copies. Two guarantees:
 * - The latest `fetcher` is held in a ref and the effect depends on `[ms, wake]`
 *   ALONE, so a caller passing an inline lambda re-renders the ref, not the
 *   effect — it can never tear down/restart the interval into a fetch loop.
 * - Every tick carries a sequence number; a slow response that resolves AFTER a
 *   newer tick's response already landed is discarded (and teardown flips `alive`,
 *   killing the old closure's stragglers), so state never moves backwards in time.
 *
 * `wake` (feed it `useEventWake()`) rides the same path: a changed value tears
 * down and re-creates the interval, which fires the immediate first tick — an
 * instant fetch plus a fresh `ms` cadence from the wake moment. `ms` stays the
 * floor, so a dead wake source degrades to plain polling.
 *
 * A changed FETCHER alone does NOT trigger a refetch: new params captured in
 * an inline lambda update the ref, but no tick fires — a facet/window flip
 * would show the old params' data under the new label for up to `ms`. The
 * sanctioned idiom is `paramsKey`: pass a stable string/number derived from
 * the fetcher's params (e.g. `${facet}|${win}`). On a paramsKey change the
 * state resets to null FIRST — the honest drop: wrong-params data must never
 * render under the new label while the fresh fetch is in flight — then an
 * immediate tick fires with a fresh `ms` cadence. (This replaces the Phase-2
 * key-remount idiom, which also wiped unrelated sibling state — form input,
 * scroll — wherever the key sat above it.) A wake bump deliberately does NOT
 * reset state: it is a refetch trigger, not a params change. */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  ms: number,
  wake = 0,
  paramsKey: string | number = '',
): Polled<T> {
  const [state, setState] = useState<Polled<T>>({
    data: null,
    error: null,
    lastFetched: null,
  })

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher // always the latest; the effect reads through the ref
  const lastParamsRef = useRef(paramsKey)

  useEffect(() => {
    if (lastParamsRef.current !== paramsKey) {
      lastParamsRef.current = paramsKey
      // The honest drop (see the contract above). Only a PARAMS change blanks
      // the panel — ms/wake re-runs keep the last good data on screen.
      setState({ data: null, error: null, lastFetched: null })
    }
    let alive = true
    let issued = 0 // ticks fired
    let applied = 0 // newest tick whose response has landed in state
    const tick = () => {
      const seq = ++issued
      fetcherRef.current().then(
        (data) => {
          if (!alive || seq < applied) return // a newer response already landed
          applied = seq
          setState({ data, error: null, lastFetched: new Date() })
        },
        (err: unknown) => {
          if (!alive || seq < applied) return
          applied = seq
          setState((prev) => ({
            ...prev,
            error: err instanceof Error ? err.message : String(err),
          }))
        },
      )
    }
    tick()
    const id = setInterval(tick, ms)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [ms, wake, paramsKey])

  return state
}

/** Subscribe to the SSE wake channel (/api/events). Returns a counter that
 * increments on every `change` event — thread it into `usePolling`'s `wake`
 * param to trigger an immediate refetch.
 *
 * Contract (cockpit/api.py): the FIRST event always fires on (re)connect, so an
 * event is only ever a refetch TRIGGER, never evidence something changed. That
 * means mount bumps the counter 0→1 and causes one immediate re-poll shortly
 * after the first — harmless (usePolling's seq guards) and correct: a reconnect
 * may mean missed changes. Deliberately no error handling: EventSource
 * auto-reconnects, and the poll floor covers a dead channel silently — that is
 * the designed degradation, not a UI-worthy fault. The "data as of" clock keeps
 * deriving from `Polled.lastFetched`; this hook only wakes fetches. */
export function useEventWake(): number {
  const [wake, setWake] = useState(0)

  useEffect(() => {
    const es = new EventSource('/api/events')
    es.addEventListener('change', () => setWake((w) => w + 1))
    return () => es.close()
  }, [])

  return wake
}
