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
/** One currently-RUNNING paper trade from the open research book (baseline arm /
 * default variant — the slice reflection grades, deduped of the arm×variant fill
 * multiplication). Live fields null with `quote_error` (class name only) when the
 * quote read failed — one bad row never blanks the panel. */
export interface OpenBookRow {
  id: number
  ticker: string
  play_type: string
  strength: string | null
  conviction_tier: string | null
  opened_date: string
  age_days: number
  entry: number
  stop: number
  target: number
  risk: number
  last_close: number | null
  unrealized_r: number | null
  unrealized_pct: number | null
  to_stop_r: number | null
  to_target_r: number | null
  quote_error: string | null
}

/** GET /api/books/open — the browsable open forward book (the running-paper-
 * trades surface). `book_label` states the slice honestly; prices ride the same
 * QuoteCache as /api/positions ("as of last close"). */
export interface OpenBook {
  rows: OpenBookRow[]
  count: number
  as_of: string | null
  book_label: string
}

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

/* ---------- Metrics scoreboard wire shapes (cockpit/scoreboard.py) ---------- */

/** A book's accounting unit: 'R' (risk multiples) or '$' (robinhood premium — no
 * stop means no R). */
export type ScoreboardUnit = 'R' | '$'

/** The R-stats shared by every scoreboard tile AND the combined pool — the wire form
 * of scoreboard.py `_r_body()`. One base so a card and the pool share a compiler tie
 * (Tasks 7-8 render both). `expectancy` is a full Stat only for an R book WITH closes;
 * a $-only or empty book sends null (an honest-empty face, never a zeroed Stat).
 * `profit_factor` is null for a $-only book or an all-winner book (JSON has no
 * Infinity). `equity_r` is null where the book has no closes. */
export interface ScoreboardStats {
  expectancy: Stat | null
  win_rate: number
  n_wins: number
  n_losses: number
  n_closed: number
  profit_factor: number | null
  /** Cumulative realized R by ascending close date, as (ISO date, cum R) pairs. */
  equity_r: [string, number][] | null
}

/** One book's scoreboard tile — the wire form of scoreboard.py `BookCard.as_dict()`.
 * `realized_usd` is null where the book has no dollars (an R-only or empty book). */
export interface ScoreboardCard extends ScoreboardStats {
  book: 'manual_equity' | 'robinhood' | 'live' | 'paper'
  unit: ScoreboardUnit
  realized_usd: number | null
}

/** The ONE sanctioned cross-book aggregate — the real-money combined pool (always unit
 * 'R'). Unlike a card's, `realized_usd` is ALWAYS a number here (manual $ plus live $,
 * live defaulting to 0) — never null. `books` is left as `string[]`: the wire lists the
 * pooled book names, not a closed union. */
export interface ScoreboardCombined extends ScoreboardStats {
  books: string[]
  unit: 'R'
  realized_usd: number
}

/** The Metrics scoreboard: one card per book plus the ONE sanctioned cross-book
 * aggregate. `combined` = manual_equity + live (both real money, both R — North Star
 * #2); firewalled journal books are never pooled, and robinhood ($-only) enters no R
 * pool. */
export interface Scoreboard {
  cards: ScoreboardCard[]
  combined: ScoreboardCombined
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
  /** Journal v2: the discretionary entry emotion (FOMO, calm…) — manual only. */
  emotional_state?: string | null
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
  /** The execution mode the caps were computed under. Under 'off' the
   * concurrent count is the DISPLAYED accounts (manual + live), never the
   * research shadow grid — caption it with the mode. Optional: older payloads. */
  mode?: string
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
  requested_at: string
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
  // Who drains the queue — the exact two strings `_worker_label` returns: the
  // cloud value (an Azure DB, drained every 15 min) or 'manual' (requests wait
  // for a manual `python -m swing_screener.notify.ondemand` run). A real union,
  // not a magic literal, so the AnalysisPanel branch is type-checked.
  worker: 'manual' | 'cloud (*/15min)'
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
  /** The three-artifact promotion steps, served on status=='approved' rows so
   * the checklist survives the transient approve response (and app restarts). */
  promotion_checklist?: string[] | null
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
  sent_at: string
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
  /** Which book backs the row (the hunch-display disambiguation): closed
   * forward-book n vs replay-corpus n. Optional — older sidecars lack them. */
  n_forward?: number | null
  n_replay?: number | null
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

/** One open research PR (reflection edge-file update / optimizer config) —
 * the learning loop's GitHub-side accept/reject gate, surfaced so the strip
 * can say so. Empty when SWING_GH_TOKEN/REPO are unset or the poll failed. */
export interface ResearchPr {
  title: string
  url: string
  kind: 'reflection' | 'optimizer'
}

/** The Needs-Your-Hand strip's permanent-poll feed — cheap file+DB reads only.
 * Corrupt stores degrade QUIETLY here (the loud markers live on the screens).
 * The audit/coach/PR fields are optional so a newer frontend degrades cleanly
 * against an older backend payload (they simply don't render). */
export interface Attention {
  proposals_queued: string[]
  /** Approved rows whose name is NOT yet in edge/experiments.json — a name that
   * reaches the registry counts as promoted and leaves this list. */
  proposals_approved_pending: string[]
  reflection_due: string[]
  /** Compare to the localStorage last-seen id for the unread badge (client-side). */
  latest_analysis_id: number | null
  /** Unacknowledged System-Audit breach rows awaiting the human ACK. */
  audit_unacked?: number
  audit_worst?: 'info' | 'warn' | 'alert' | null
  /** Coach reviews awaiting a human tag-confirm (Journal screen). */
  coach_pending?: number
  research_prs?: ResearchPr[]
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

export const getOpenBook = (facet: Facet = 'research'): Promise<OpenBook> =>
  fetchJson<OpenBook>(`/api/books/open?facet=${facet}`)

/** POST /api/experiments/{name}/decide result — decide MARKS the registry
 * (uncommitted working-tree edit); the checklist is the human half (roster
 * line + one commit). */
export interface ExperimentDecided {
  name: string
  status: string
  decided_at: string | null
  note: string
  checklist: string[]
}

export const getFunnel = (): Promise<FunnelResponse> => fetchJson<FunnelResponse>('/api/funnel')

export const getPerformance = (
  playType: PlayType,
  win: Window, // `window` would shadow the global — `win` everywhere in here
  facet: Facet,
): Promise<Performance> =>
  fetchJson<Performance>(
    `/api/stats/performance?play_type=${playType}&window=${win}&facet=${facet}`,
  )

/** The cross-book Metrics scoreboard, windowed by CLOSE date. Reuses the `Window`
 * union — the Python endpoint's Literal["all","90","180","365"] matches it 1:1 — and
 * names the param `win`, never `window` (the global-shadow rule; see `Window`). */
export const getScoreboard = (win: Window): Promise<Scoreboard> =>
  fetchJson<Scoreboard>(`/api/stats/scoreboard?window=${win}`)

export const getGate = (): Promise<Gate> => fetchJson<Gate>('/api/gate')

/** GET /api/config — the read-only configuration panel: every live knob with
 * its env var, current value, and where the REAL edit lives (the cockpit never
 * writes config — North Star #1/#3; env is per-process, so a cockpit edit
 * could not reach the Azure jobs anyway). */
export interface ConfigRow {
  key: string
  env: string | null
  value: unknown
  note: string
}

export interface ConfigSection {
  title: string
  change_via: string
  rows: ConfigRow[]
}

export interface CockpitConfig {
  env_scope: string
  sections: ConfigSection[]
}

export const getConfig = (): Promise<CockpitConfig> =>
  fetchJson<CockpitConfig>('/api/config')

/** POST an action with the X-Cockpit guard header (the one mutation guard — it
 * forces cross-origin callers into a failing CORS preflight; same-origin us
 * attaches it trivially). Backend errors surface their one safe {detail} line
 * via fetchJson. Every action fetcher routes here — an action POST without the
 * header is a 403 by construction. `signal` lets a caller cancel a request it
 * no longer wants (an aborted fetch rejects with an AbortError DOMException —
 * a caller that passes one owns that rejection). */
const postAction = <T>(
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

/** Retire a settled/futile experiment — decide MARKS edge/experiments.json
 * (working-tree edit + audit fields); the roster deletion + commit stay human
 * (the response checklist). 404 unknown, 409 already decided. */
export const postDecideExperiment = (
  name: string,
  reason: string,
): Promise<ExperimentDecided> =>
  postAction<ExperimentDecided>(
    `/api/experiments/${encodeURIComponent(name)}/decide`,
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

/* ---------- Phase 3 wire shapes (routers/journal.py) ---------- */

/** The journal's per-account firewall: one book selects one account. Distinct
 * from the masthead `Facet` (research/gold) — these are the trade accounts.
 * research/paper/live are the MACHINE shadow-grid books; manual_equity/robinhood
 * are Oliver's real PERSONAL books (the Coach's scope, never the machine's). */
export type JournalBook =
  | 'research'
  | 'paper'
  | 'live'
  | 'manual_equity'
  | 'robinhood'

/** A personal book — the only books the Coach voice is applied to. */
export type CoachBook = 'manual_equity' | 'robinhood'

/** One P&L calendar cell: summed R (null under the non-finite→null rule) + count.
 * Shared by the day grid and the month roll-up. */
export interface CalendarCell {
  r: number | null
  n: number
}

/** GET /api/journal/calendar — days keyed by ISO date, months by "YYYY-MM".
 * `cost_level` is the BOOK's vintage stamp (null = mixed/unstamped), never
 * per-cell. With no `month` param `days` spans every closed day. */
export interface JournalCalendar {
  days: Record<string, CalendarCell>
  months: Record<string, CalendarCell>
  cost_level: string | null
}

/** GET /api/journal/curve — the realized equity curve, its underwater series,
 * and max drawdown, all in R. Points are [ISO date, value] with value null
 * under the non-finite→null rule (a caller filters nulls before plotting). */
export interface JournalCurve {
  curve: [string, number | null][]
  drawdown: [string, number | null][]
  max_drawdown: number | null
}

/** GET /api/journal/excursions — MAE/MFE means + medians in R (descriptive
 * floats, NOT Stats: no CI machinery behind an excursion mean). null = the
 * cohort had no instrumented rows to measure. */
export interface JournalExcursions {
  n: number
  avg_mae_r: number | null
  avg_mfe_r: number | null
  median_mae_r: number | null
  median_mfe_r: number | null
}

/** The breakdown axis — day-of-week / hold-time / symbol. */
export type BreakdownBy = 'dow' | 'hold' | 'symbol'

/** GET /api/journal/breakdowns — each bucket is a full Stat (design rule 1),
 * keyed by its label (Mon..Fri, hold ranges, or ticker) in server order. */
export interface JournalBreakdowns {
  buckets: Record<string, Stat>
}

/** GET /api/journal/discipline — swing discipline metrics with their counts.
 * The three metric floats are None-safe (null = unmeasured cohort);
 * `stop_honored_rate` is a 0..1 fraction, the two R metrics are R. Counts are
 * structural ints. */
export interface JournalDiscipline {
  giveback_r: number | null
  stop_honored_rate: number | null
  avg_mae_before_win: number | null
  n_closed: number
  n_with_excursion: number
  n_wins: number
  n_stopped: number
  n_with_exit_reason: number
}

/** One GET /api/journal/mistakes row (worst-first): `total_r`/`n` are the plain
 * aggregate cost + count (like a KPI), while `stat` is the per-trade expectancy
 * as a full Stat so the edge claim carries its CI. */
export interface MistakeRow {
  mistake: string
  n: number
  total_r: number | null
  stat: Stat
}

/** A note's slot in the trading day (the wire-constrained enum). */
export type NoteKind = 'premarket' | 'postmarket' | 'adhoc'

/** GET /api/journal/notes row. `source` is stamped server-side ('human' for a
 * cockpit-written note); `module` is optional free text. */
export interface JournalNote {
  id: number
  day: string
  kind: NoteKind
  module: string | null
  body: string
  source: string
  created_at: string
}

/** POST /api/journal/notes body — `source` is NOT a field (stamped 'human'
 * server-side; a client can never claim screener/analyst provenance). */
export interface NoteCreate {
  day: string
  kind: NoteKind
  body: string
  module?: string | null
}

/** A tag as displayed on a record (read-only here — the tagging UI is deferred). */
export interface JournalTagView {
  name: string
  kind: string
  source: string
}

/** A thesis as displayed on a record: which event, who wrote it, the prose. */
export interface JournalThesisView {
  event_kind: string
  source: string
  body: string
}

/** One GET /api/journal/records row — a display TradeRecord with its tags +
 * theses. Display only, never an aggregate. `opened`/`closed` are ISO (closed
 * null while open); `r` is realized R (null until closed); the swing book is
 * long-only (`direction: 'long'`, `unit: 'R'`). */
export interface TradeRecordRow {
  trade_id: number
  book: string
  module: string
  symbol: string
  direction: string
  opened: string | null
  closed: string | null
  unit: string
  r: number | null
  tags: JournalTagView[]
  theses: JournalThesisView[]
}

/** The machine books' evaluation slice: 'baseline' (one row per screened
 * candidate — the slice reflection grades; the honest default) or 'grid' (the
 * full arm×variant tournament pool — sums POOL the grid; label it). Personal
 * books ignore it server-side. */
export type JournalScope = 'baseline' | 'grid'

export const getJournalCalendar = (
  book: JournalBook,
  month?: string,
  scope: JournalScope = 'baseline',
): Promise<JournalCalendar> =>
  fetchJson<JournalCalendar>(
    month === undefined || month === ''
      ? `/api/journal/calendar?book=${book}&scope=${scope}`
      : `/api/journal/calendar?book=${book}&month=${month}&scope=${scope}`,
  )

export const getJournalCurve = (
  book: JournalBook,
  scope: JournalScope = 'baseline',
): Promise<JournalCurve> =>
  fetchJson<JournalCurve>(`/api/journal/curve?book=${book}&scope=${scope}`)

export const getJournalExcursions = (
  book: JournalBook,
  scope: JournalScope = 'baseline',
): Promise<JournalExcursions> =>
  fetchJson<JournalExcursions>(`/api/journal/excursions?book=${book}&scope=${scope}`)

export const getJournalBreakdowns = (
  book: JournalBook,
  by: BreakdownBy,
  scope: JournalScope = 'baseline',
): Promise<JournalBreakdowns> =>
  fetchJson<JournalBreakdowns>(
    `/api/journal/breakdowns?book=${book}&by=${by}&scope=${scope}`,
  )

export const getJournalDiscipline = (
  book: JournalBook,
  scope: JournalScope = 'baseline',
): Promise<JournalDiscipline> =>
  fetchJson<JournalDiscipline>(`/api/journal/discipline?book=${book}&scope=${scope}`)

export const getJournalMistakes = (
  book: JournalBook,
  scope: JournalScope = 'baseline',
): Promise<MistakeRow[]> =>
  fetchJson<MistakeRow[]>(`/api/journal/mistakes?book=${book}&scope=${scope}`)

/** Newest-first, paginated: `total` counts the whole cohort so the panel can
 * say "newest N of M" instead of silently truncating. */
export interface JournalRecords {
  records: TradeRecordRow[]
  total: number
  scope: string
}

export const getJournalRecords = (
  book: JournalBook,
  scope: JournalScope = 'baseline',
  limit = 200,
): Promise<JournalRecords> =>
  fetchJson<JournalRecords>(
    `/api/journal/records?book=${book}&scope=${scope}&limit=${limit}`,
  )

/** Notes are DAY-scoped, not book-scoped (a bad date is the server's 422). */
export const getJournalNotes = (day: string): Promise<JournalNote[]> =>
  fetchJson<JournalNote[]>(`/api/journal/notes?day=${day}`)

/** Write a human notebook entry (X-Cockpit guarded; `source` stamped server-side). */
export const postJournalNote = (body: NoteCreate): Promise<JournalNote> =>
  postAction<JournalNote>('/api/journal/notes', body)

/* ---------- Journal v2 Coach wire shapes (routers/coach.py) ---------- */

/** One parked auto-tag proposal (lives in a review's facts until confirmed). */
export interface TagProposal {
  name: string
  kind: string
  reason: string
}

/** One Coach review of a real personal trade. `facts` is the code-owned scorecard
 * (authoritative); `narrative` is advisory LLM prose (null until the async draft
 * lands); `human_edit` is Oliver's own edit. Displayed figures come from `facts`. */
export interface CoachReview {
  id: number
  kind: string
  book: CoachBook
  trade_id: number | null
  generated_at: string | null
  model: string | null
  est_cost_usd: number | null
  narrative: string | null
  human_edit: string | null
  facts: {
    result?: number | null
    unit?: string
    outcome?: string
    hold_days?: number | null
    moved_stop?: boolean
    override?: string | null
    emotional_state?: string | null
    tag_proposals?: TagProposal[]
    [k: string]: unknown
  }
}

/** The current Weaknesses Profile (a distillation, staleness-stamped). */
export interface WeaknessesProfile {
  items: { weakness: string; count?: number; evidence?: { book: string; trade_id: number }[] }[]
  thin_data: boolean
  n_reviews: number
  generated_at: string | null
}

export const getCoachReviews = (book: CoachBook): Promise<CoachReview[]> =>
  fetchJson<CoachReview[]>(`/api/coach/reviews?book=${book}`)

export const getWeaknesses = (): Promise<WeaknessesProfile> =>
  fetchJson<WeaknessesProfile>('/api/coach/weaknesses')

/** Save Oliver's edited prose onto a review (X-Cockpit guarded). */
export const postCoachEdit = (id: number, humanEdit: string): Promise<CoachReview> =>
  postAction<CoachReview>(`/api/coach/reviews/${id}/edit`, { human_edit: humanEdit })

/** Confirm a parked proposal -> the overlay tag (source=analyst) is written. */
export const postConfirmTag = (
  id: number,
  tag: { name: string; kind: string },
): Promise<{ applied: { name: string; kind: string }; trade_id: number }> =>
  postAction(`/api/coach/reviews/${id}/confirm-tag`, tag)

/* ---------- Journal v2 Auditor wire shapes (routers/audit.py) ---------- */

/** One System Behavior Audit — a weekly conduct report or an immediate breach.
 * `findings` is the code-owned conduct scorecard; `narrative` advisory prose. */
export interface AuditReport {
  id: number
  kind: string
  period_from: string
  period_to: string
  breach_key: string
  severity: string
  acknowledged: boolean
  generated_at: string | null
  model: string | null
  est_cost_usd: number | null
  narrative: string | null
  findings: Record<string, unknown>
}

export const getAuditReports = (): Promise<AuditReport[]> =>
  fetchJson<AuditReport[]>('/api/audit/reports')

export const getAuditBreaches = (): Promise<AuditReport[]> =>
  fetchJson<AuditReport[]>('/api/audit/breaches')

/** Acknowledge a report/breach (X-Cockpit guarded, no body). */
export const postAuditAck = (id: number): Promise<AuditReport> =>
  postAction<AuditReport>(`/api/audit/${id}/ack`)

/* ---------- GEX lab wire shapes (routers/gex.py) ---------- */

/** One per-strike dollar-gamma row of the profile chart (calls ≥ 0, puts ≤ 0;
 * net = call_gex + put_gex, computed client-side). */
export interface GexStrike {
  strike: number
  call_gex: number
  put_gex: number
}

/** One GEX snapshot — deterministic level facts, so plain nullable numbers, NOT
 * Stats (a level has no n / CI / provenance). A null level renders an em dash.
 * `profile` is null when the stored blob is absent/corrupt (chart degrades,
 * levels still render); `reading` is the deterministic what-this-means lines
 * (options/reading.py — thin warning first, model-honesty line last). */
export interface GexSnapshot {
  underlying: string
  ts: string
  spot: number | null
  call_wall: number | null
  put_wall: number | null
  gamma_flip: number | null
  regime: string
  thin_chain: boolean
  net_gex?: number | null
  profile?: GexStrike[] | null
  reading?: string[]
}

export interface GexPlanResponse {
  snapshots: GexSnapshot[]
  watchlist: string[]
}

/** One built DayPlan row (POST plan/build with no ticker). bias is
 * bullish|bearish|tangled, call is breakout|range|stand_down; spacing_pct is
 * already a percent number (not a fraction). */
export interface GexDayPlan {
  underlying: string
  bias: string
  regime: string
  call: string
  spacing_pct: number | null
  call_wall: number | null
  put_wall: number | null
  gamma_flip: number | null
  spot: number | null
}

/** Single-ticker analyze result (POST plan/build with a ticker). thin_reasons
 * are the loud per-reason strings when thin_chain — levels are unreliable. */
export interface GexAnalyzed {
  underlying: string
  regime: string
  call_wall: number | null
  put_wall: number | null
  gamma_flip: number | null
  spot: number | null
  thin_chain: boolean
  thin_reasons: string[]
  net_gex?: number | null
  profile?: GexStrike[]
  reading?: string[]
}

/** plan/build returns EITHER {plans} (no ticker) OR {analyzed} (a ticker) —
 * exactly one branch is present; the caller narrows on which. */
export interface GexBuildResult {
  plans?: GexDayPlan[]
  analyzed?: GexAnalyzed
}

export type GexGrade = 'A+' | 'B' | 'no_trade'
export type GexStatus = 'idea' | 'taken' | 'skipped'

/** The GEX play type — the machine regime rule keys on it (breakout wants
 * negative gamma, range positive); '' is "unset" (the regime item then reads
 * needs_input). Distinct from the equity `PlayType` (continuation/reversal). */
export type GexPlayType = 'breakout' | 'range' | ''

/** The 12 checklist keys — the exact OptionSetup.chk_* columns (checklist.py). */
export interface GexChecklist {
  chk_daily_bias_clear: boolean
  chk_daily_stack_ordered: boolean
  chk_m5_agrees: boolean
  chk_gex_levels_marked: boolean
  chk_price_at_pivot: boolean
  chk_regime_match: boolean
  chk_pattern_clean: boolean
  chk_volume_confirming: boolean
  chk_risk_sized: boolean
  chk_stop_structural: boolean
  chk_rr_at_least_2: boolean
  chk_confirmation_candle: boolean
}

/** One journaled setup — the checklist booleans ride inline (spread into the
 * row server-side). grade is the graded verdict ("A+"|"B"|"no_trade"). */
/** The linked lab paper trade's outcome, joined onto its setup row — a taken
 * setup finally answers "was I right?" without the CLI. null = no trade
 * (idea/skipped, or the take predates the link). */
export interface GexSetupTrade {
  status: 'open' | 'closed'
  opened_at: string
  exit_reason: string | null
  realized_r: number | null
}

export interface GexSetup extends GexChecklist {
  id: number
  ts: string
  underlying: string
  direction: string
  regime: string
  /** breakout | range | '' — the machine-vs-human discipline facet's play axis
   * (server `_setup_dict` returns it; '' on rows journaled before play types). */
  play_type: string
  grade: string
  status: string
  pattern: string
  notes: string
  pivot_level: number | null
  entry: number | null
  stop: number | null
  target: number | null
  trade?: GexSetupTrade | null
}

export interface GexSetupsResponse {
  setups: GexSetup[]
}

/** POST /api/gex/setups body — the checklist carries all 12 keys; the server
 * grades it (422 on an unknown/missing key, on junk play_type, or on a ticked
 * R:R that contradicts the typed levels). */
export interface GexSetupCreate {
  underlying: string
  direction: string
  checklist: GexChecklist
  entry?: number | null
  stop?: number | null
  target?: number | null
  regime?: string
  /** breakout | range | '' — the write path enforces the same enum the autograde
   * read does (422 on junk); '' is a valid "no play type". */
  play_type?: GexPlayType
  pivot_level?: number | null
  pattern?: string
  notes?: string
  /** The last autograde response's provenance blob, echoed back VERBATIM (null /
   * omitted when the grader never ran) — stored opaquely, never reconstructed
   * client-side (it is the machine's record, not the form state). */
  autograde_json?: string | null
}

/** One machine-graded checklist item (POST /api/gex/autograde) — `state` mirrors
 * options/autograde.py's per-item verdict; `fact` is the single line that decided
 * it (the number, never a restatement of the rule). */
export interface GexAutogradeItem {
  key: string
  state: 'pass' | 'fail' | 'needs_input' | 'unavailable'
  fact: string
}

/** POST /api/gex/autograde response — the eight machine items (MACHINE_KEYS
 * order), the two advisory hints (pivot, then stop), the whole-checklist
 * `machine_verdict` (any fail → 'no'; all pass → 'yes'; a gap → 'incomplete'),
 * the opaque `autograde_json` provenance the FE echoes back VERBATIM on the
 * setups POST (never constructed client-side), and the same-day `snapshot` the
 * read graded against (null when none is). */
export interface GexAutogradeResponse {
  underlying: string
  machine_verdict: 'yes' | 'no' | 'incomplete'
  items: GexAutogradeItem[]
  hints: string[]
  autograde_json: string
  snapshot: GexSnapshot | null
}

/** POST /api/gex/autograde body — underlying required (422 when blank),
 * direction long|short, play_type breakout|range|''; the levels are optional
 * floats the pure grader degrades to needs_input rather than fabricating. */
export interface GexAutogradeBody {
  underlying: string
  direction: string
  play_type: GexPlayType
  entry: number | null
  stop: number | null
  target: number | null
  pivot_level: number | null
}

/** A lab Stat with its checklist grade label (GET /api/gex/stats by_grade). */
export interface GexGradeStat extends Stat {
  grade: string
}

/** One Robinhood strategy tag's premium book — PLAIN labeled values, NOT a Stat
 * (premium dollars, no CI, no R). total_pnl sums premium P&L; open counts open
 * rows. */
export interface RobinhoodBook {
  n: number
  total_pnl: number
  wins: number
  losses: number
  open: number
}

/** GET /api/gex/stats. `robinhood` is keyed by strategy tag ("gex"/"other") —
 * a tag with no imported rows is simply ABSENT, never a zeroed book. */
export interface GexStats {
  overall: Stat
  by_grade: GexGradeStat[]
  robinhood: Record<string, RobinhoodBook>
  /** Open (unsettled) lab paper trades — the settle sweep's pending work. */
  open_trades?: number
}

/** POST /api/gex/settle — the idempotent settle sweep (the CLI ritual, now a
 * button): grades due open lab trades; 200 with settled=0 when nothing is due. */
export interface GexSettleResult {
  settled: number
  open_remaining: number
}

/** One paired import episode (open→close of an occ contract). needs_review
 * flags an episode the pairer could not fully reconcile. import_key is the
 * commit-tag join key. */
export interface GexEpisode {
  occ_symbol: string
  underlying: string
  opened_on: string
  closed_on: string | null
  status: string
  contracts: number
  entry_premium: number | null
  exit_premium: number | null
  pnl: number | null
  exit_reason: string | null
  needs_review: boolean
  import_key: string
}

export interface GexParseResult {
  episodes: GexEpisode[]
  fills_added: number
  fills_skipped: number
}

export interface GexCommitResult {
  committed: number
}

export const getGexPlan = (): Promise<GexPlanResponse> =>
  fetchJson<GexPlanResponse>('/api/gex/plan')

/** Build the day's plans (no ticker) OR analyze one ticker — the response
 * branch (plans vs analyzed) follows the argument. Both touch the network
 * server-side (a yfinance chain fetch), so this can be slow. */
export const postGexBuild = (ticker?: string): Promise<GexBuildResult> =>
  postAction<GexBuildResult>(
    '/api/gex/plan/build',
    ticker !== undefined && ticker.trim() !== '' ? { ticker: ticker.trim() } : {},
  )

export const getGexSetups = (
  day?: string,
  recent = false,
): Promise<GexSetupsResponse> =>
  fetchJson<GexSetupsResponse>(
    recent
      ? '/api/gex/setups?recent=1'
      : day === undefined || day === ''
        ? '/api/gex/setups'
        : `/api/gex/setups?day=${encodeURIComponent(day)}`,
  )

/** Journal a graded setup — the server grades the checklist and returns the row. */
export const postGexSetup = (body: GexSetupCreate): Promise<GexSetup> =>
  postAction<GexSetup>('/api/gex/setups', body)

/** Machine pre-grade the eight computable checklist items — decision support for
 * the A+ hypothesis, NOT the journaled grade. A READ despite the POST (the server
 * bumps no action-nonce); X-Cockpit guarded like every action. Upstream failures
 * degrade items to 'unavailable' and still return an 'incomplete' verdict. */
export const postGexAutograde = (
  body: GexAutogradeBody,
): Promise<GexAutogradeResponse> =>
  postAction<GexAutogradeResponse>('/api/gex/autograde', body)

/** Take (opens a paper trade) or skip an `idea` setup. */
export const postGexSetupStatus = (
  setupId: number,
  status: 'taken' | 'skipped',
): Promise<GexSetup> =>
  postAction<GexSetup>(`/api/gex/setups/${setupId}/status`, { status })

/** Parse a broker activity CSV into paired episodes (idempotently stores the
 * fills); the commit is a separate step over the same CSV. */
export const postGexImportParse = (csvText: string): Promise<GexParseResult> =>
  postAction<GexParseResult>('/api/gex/import/parse', { csv_text: csvText })

/** Commit the tagged episodes — `tags` maps each episode's import_key to
 * "gex" | "other" | "skip". Re-parses the same CSV server-side. */
export const postGexImportCommit = (
  csvText: string,
  tags: Record<string, string>,
): Promise<GexCommitResult> =>
  postAction<GexCommitResult>('/api/gex/import/commit', { csv_text: csvText, tags })

export const getGexStats = (): Promise<GexStats> =>
  fetchJson<GexStats>('/api/gex/stats')

/** The settle sweep (idempotent; X-Cockpit like every action). */
export const postGexSettle = (): Promise<GexSettleResult> =>
  postAction<GexSettleResult>('/api/gex/settle')

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
