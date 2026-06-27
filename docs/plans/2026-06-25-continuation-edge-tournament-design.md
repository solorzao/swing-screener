# Continuation edge tournament (design)

2026-06-25

## Motivation

Our Heiken-Ashi pullback-continuation book has no edge (replay: ~-0.12R, ~32% win, ~76%
losers). The failure is an ENTRY problem: ~47% of trades exit via an immediate bearish
momentum-flip at ~-0.32R; only ~16% reach target. The current ranking score is only weakly
predictive (top bucket still negative). The reversal edge was found the same way we will hunt
here: the edge lived in a high-conviction SUBSET (CONFIRMED), not the average.

Pullback-continuation is a sound, common method; the hypothesis is that *our implementation*
lacks a quality filter that isolates the tradeable subset. A parallel research pass
(scripts + the `continuation-edge-research` workflow) produced a slate of candidate edge
dimensions; round 1 tests the cheapest, highest-conviction ones competitively.

## Round 1 — Tier-A quality gates (this change)

Eight detection-only `StrategyConfig` levers, all default no-op, each computed from the
existing enriched frame (no new indicator periods, so all are legal screen variants):

| gate | rule | dimension |
|---|---|---|
| `vol_thrust_min` | trigger volume / mean(volume, `vol_avg_window`) ≥ X | volume confirmation |
| `require_value_band` | pullback low reaches EMA20 band (`band_touch_tol_atr`) AND holds above EMA50 (`band_floor_buf_atr`) | depth-to-value |
| `require_orderly_pullback` | no pullback bar > `max_pullback_bar_atr`; drop ≤ `max_pullback_drop_atr` | shape |
| `min_ema_sep_atr` | (EMA20−EMA50)/ATR ≥ X | trend strength |
| `require_macd_hook` | macd_hist > 0 AND rising | momentum |
| `rsi_min_trigger` | trigger RSI ≥ X | momentum |
| `min_atr_pct` | ATR/close ≥ X (drops low-vol names) | volatility selection |
| `min_trigger_body_frac` | trigger HA body_frac ≥ X (+ shaved bottom / small lower wick) | trigger quality |

Logic lives in `signals/detect.py::_quality_gates_pass`, called after the swing-low / shallow
gates. Each is unit-tested (`tests/signals/test_detect_gates.py`) for no-op-off + reject-on-violation.

## Evaluation protocol

`scripts/replay_tournament.py` races control + the 8 gates through the golden-master
`replay_book`, compared CONTINUATION-ONLY (reversal trades are identical across variants and
would dilute the leaderboard), reporting expectancy + 95%-low + the momentum_flip share.
Then: combine the gates that move expectancy toward 0 (stacked on `value_band`, the prior
winner), and slippage-sweep the survivors (fill_slippage_atr 0.00/0.05/0.10). The bar for any
promotion is the reversal bar: a positive, cost-robust SUBSET (95%-low > 0 at ~0.05 ATR),
not rescuing the whole book.

## Non-goals / round 2 (deferred)

Conviction tiers (ADX, Fib-depth, RSI reset-and-turn, no-bearish-divergence) and
plumbing-heavy levers (SPY-regime gate — regime is stamped on fills but not in the detector;
capitulation-flush that relaxes the shallow gate and deepens the stop; hold-the-low 2-bar
trigger that shifts the entry anchor). Pursue only if round 1 points there.
