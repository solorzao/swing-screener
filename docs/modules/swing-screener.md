# Swing Screener — Module Charter

**Module 1 of [Meridian](../NORTH_STAR.md).** This charter rules the module's scope; the
[North Star](../NORTH_STAR.md) principles bind it; the
[module contract](../ARCHITECTURE.md) defines what it provides.

## Scope

- **Strategies (play types):** long-only **trend-continuation pullbacks** and
  **oversold-bounce reversals**, detected on Heiken Ashi candles with EMA/ATR/RSI/MACD context.
- **Instruments:** US equities from a curated ~500-ticker universe.
- **Horizon: swing only — days to weeks. Not day-trading.** (This restriction is this
  module's scope, moved here from the suite North Star when Meridian became a suite;
  intraday work belongs to other modules, e.g. the [GEX lab](gex-lab.md).)
- **Timeframes:** daily / weekly / monthly / 4h — completed bars only, evaluated in nightly
  batch. The completed-bar invariant is load-bearing at three layers; nothing in this module
  evaluates bars during market hours — intraday, only the hourly exit check and the
  on-demand deep-analysis queue worker run.

## Module specifics

- **Config:** `StrategyConfig` (frozen dataclass, `src/swing_screener/config.py`) — every
  knob carries experiment provenance; DETECTION-only fields are the legal screen-variant
  sweep space.
- **Books:** `research` (shadow book: every screened signal, arm × variant experiment
  facets, `would_surface` gold facet), `paper` (the curated intent book — the paper adapter's
  simulated fills), `live`, plus `manual` for recorded human-placed tickets. Shadow fills are
  pessimistic (worst-case in-zone).
- **Statistics:** R-multiples net of costs, **clustered by ticker** (8-distinct-ticker
  floor), corpus-pinned. These constants are this module's; other modules declare their own
  cluster keys and never relax this one.
- **Playbooks:** [`edge/continuation.md`](../../edge/continuation.md) and
  [`edge/reversal.md`](../../edge/reversal.md) (+ code-owned `*.verdicts.json`) — the living
  record of confirmed edges, hunches, and falsified ideas. Verdict state lives THERE, not in
  this charter.
- **Execution:** the module's order intents run through the suite execution arc
  (`manual` / `paper` / `alpaca` behind three locks + caps). Today this machinery physically
  lives inside the module; it is promoted to the platform when a second module earns
  execution (see ARCHITECTURE's extraction policy).

## Operational posture

Seven scheduled Azure Container Apps jobs (evening screen, daily/weekly/monthly digests,
hourly exit checks, the hourly on-demand deep-analysis worker, Sunday market weather) plus
the local cockpit. Default execution posture
is `off`; real money requires a deliberate human arm with independent locks.

## Non-goals

- Day-trading or intraday evaluation of any kind.
- Short setups (long-only by design, revisit only with evidence machinery ready).
- Widening the detection search space beyond the pre-registered variant machinery.
