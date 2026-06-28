# Market screener — weekly macro "Market Weather" report (design)

2026-06-26

## Motivation

A market-broad screener (not a stock pick): watch how the market is moving in general — trend
shifts, rotations, risk build-up. Weekly cadence, watching SPY, with a deep LLM analysis →
report, mirroring the stock analyst's architecture (deterministic facts → Opus + web-search →
structured report → deterministic fallback).

## Decisions (brainstorm)
- **Hybrid data**: fetch the reliable series deterministically; let the analyst web-search the
  awkward macro inputs.
- **Weekly always, flip emphasized**: a recurring weekly report; a regime flip / VIX spike is
  called out prominently when present.
- **Standalone "Market Weather" email** (separate from the stock-pick digest).
- **Multi-timeframe HA**: SPY Heiken-Ashi on monthly + weekly (regime anchors) and daily (the
  faster, leading signal); report whether the three AGREE, with daily-vs-weekly divergence as
  the early trend-shift warning.

## Components (new)
- `pipeline/market.py` — `gather_market_facts(...) -> MarketFacts`, pure over injectable fetch
  seams (SPY daily, ^VIX, TLT, ^TNX, ^IRX). Resamples SPY daily → weekly/monthly, builds HA per
  timeframe, classifies regime + VIX rank + yield inversion.
- `notify/market_analysis.py` — `analyze_market_deep(facts) -> MarketAnalysis`: a macro-strategist
  Opus call (mirrors `analyze_ticker_deep`) — facts as ground truth, `web_search` on, labelled
  structured output, citations, usage capture, deterministic fallback. Injectable client seam.
- `notify/market_body.py` — render the report (text + HTML); weekly entrypoint in `notify/run.py`
  (`run_market_report`) → gather → analyze → send (existing smtp/transport) → persist.
- `db/models.py` `MarketReport` + Alembic migration — one row per weekly run (history + flip log).

## Deterministic facts (`MarketFacts`)
- **SPY HA multi-timeframe**: for monthly, weekly, daily — color (bull/bear), flipped-vs-prior,
  bars-in-state. Plus `ha_alignment` ∈ {aligned_bull, aligned_bear, mixed} and a short
  human description of which timeframes agree/diverge.
- SPY vs 200DMA (trend) + ATR% vol bucket (`regime.classify_regime`).
- **VIX**: level, percentile rank (trailing 252), spike flag (rank ≥ `vix_spike_rank`, ~80).
- **Rates**: 10y (`^TNX`/10), 3m (`^IRX`/100... per yfinance scaling), inversion sign (3m > 10y);
  **bonds**: TLT weekly HA / trend.
- Each fetch is None-safe (per-source isolation): a missing series degrades that field to None;
  the analyst + the deterministic report both handle absent fields.

## Delegated to the analyst's web search
Shiller CAPE (current), Fear & Greed index, the 2y / fuller yield curve, and major economic
news / the week's calendar. The analyst cites sources.

## Analyst output (labelled lines)
`CORE` (one-line stance) · `Regime` (MTF HA agreement + 200DMA; flip called out) · `Volatility`
(VIX rank/spike) · `Rates` (curve/inversion + bonds) · `Valuation` (Shiller) · `Sentiment`
(Fear & Greed + news) · `Rotation` (sector/style shifts) · `Risk` (the biggest) · `Watch` (key
levels/events). Informational, not financial advice.

## Gating / cost / safety
Mirror `deep_analysis`: a `market_report_enabled` flag + model/reasoning/max-searches config +
the existing `Usage`/cost-cap. If the LLM is off / over-cap / fails, send the **deterministic
facts report** (the MTF HA alignment, VIX rank, inversion, regime) — the report never blocks.

## Testing (all offline)
- `gather_market_facts`: mock the fetch seams; assert HA color + flip per timeframe, alignment
  classification, VIX spike, inversion sign, None-safe degradation.
- `analyze_market_deep`: inject a fake Anthropic client (like the existing analyst tests); assert
  labelled-line parse, citation append, and deterministic fallback on failure.
- email render: snapshot the report body.

## Non-goals (v1)
No intraday cadence; no automated trading off the report; no deterministic Shiller/F&G/2y
fetchers (delegated to web-search); no sector-rotation *computation* (the analyst reads it).
