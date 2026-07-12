# GEX Options Lab — Module Charter

**Module 2 of [Meridian](../NORTH_STAR.md).** This charter rules the module's scope; the
[North Star](../NORTH_STAR.md) principles bind it; the
[module contract](../ARCHITECTURE.md) defines what it provides. Design:
[2026-07-11-gex-options-lab-design.md](../plans/2026-07-11-gex-options-lab-design.md).

## Purpose

Learn and validate the GEX day-trading method — 9/21/50 EMA stacks for direction, dealer-gamma
levels (call wall, put wall, gamma flip) for structure, a 12-point A+ checklist for discipline
— with **paper trades and imported real trades**. The lab produces evidence, not adrenaline:
every setup is graded before its outcome is known, every trade is journaled, and the question
"does this method have edge, and am I disciplined enough to trade it" is answered with
statistics, not vibes.

## Scope

- **Strategy:** single-leg long calls/puts on index products, day-trade horizon (setups form
  and resolve within one session). This module is **deliberately intraday** — the swing
  module's horizon restriction does not apply here.
- **Universe:** SPY + QQQ watchlist for the automatic morning plan; **any optionable ticker**
  for ad-hoc analysis, behind a chain-liquidity guard that warns (never blocks) on thin
  chains where dealer-gamma reasoning breaks down.
- **GEX model:** computed in-house (chain OI × Black-Scholes gamma; naive dealer-positioning
  convention — the same model public dashboards use), morning-static (OI updates once daily),
  cross-checked against a free dashboard as a human ritual. Levels are structure, not prophecy.
- **Phase 1 is entirely local:** morning plan + cockpit journal/grader + nightly settlement
  from completed 5-minute bars. No Azure jobs, no live feed, no alerts. Phase 2 (live
  intraday engine) is designed-for but unbuilt.

## Lab rules

- **Primary metric: R-multiples on the underlying** (stop/target geometry), so setup edge is
  measured clean of theta/IV noise. Premium tracking arrives only after the setup shows edge.
- **Statistics cluster by session (trading day)** — the correct independence unit for day
  trades, and the only honest one for a 2-ticker book. The equity module's ticker-cluster
  constants are never touched or relaxed.
- **Books:** `options-lab` (paper, R-denominated) and `robinhood` (imported real trades,
  premium-denominated, review-tagged `gex`/`other` at import). Never pooled with each other
  or with equity books; the gex-vs-other comparison is a labeled display, not pooled inference.
- **The checklist grade is recorded at decision time**, before outcome — discipline metrics
  ("A+-rate", "sat on my hands") are first-class outputs, per the journal layer.
- **Playbook:** [`edge/gex.md`](../../edge/gex.md); reflection/verdicts arrive once ~20
  closed trades exist (Phase 1.5), on the suite's deterministic-grader machinery.

## Non-goals

- **No real-money execution path — none exists in this module by design.** Paper and
  imported-trade analysis only until the evidence machinery says otherwise.
- No multi-leg strategies (spreads flagged `needs review` at import, never auto-paired).
- No intraday GEX drift modeling (a Phase-2+, paid-data question).
- No prediction claims on thin chains — the liquidity guard's warning is honest output.
- No SPX until a proper index-options data source is added (CBOE).
