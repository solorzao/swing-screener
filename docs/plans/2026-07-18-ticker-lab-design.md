# Ticker Lab — the on-demand per-ticker study (design)

2026-07-18. The cockpit's fifteenth screen (`g t` · TICKER LAB): Oliver's own
research surface. Any ticker, on demand — Heiken Ashi candles on the four lab
timeframes (4h / 1d / 1wk / 1mo) with toggleable EMA 9/21/50/200, MACD(12,26,9),
volume, swing-pivot support/resistance, and Fibonacci retracement — plus a
**deep analysis** action that sends the full four-timeframe study (and
fundamentals/news context) to the Opus analyst at **max** effort.

## What it is (and is not)

The lab is a research surface, not a signal engine: it computes and renders
deterministic facts, takes no positions, writes no config, and never feeds the
screener. It shares the honesty posture of every other screen — every price is
"as of last close", every number the model sees is engine-computed ground truth,
and the leak/error postures are the cockpit's standing ones.

## The pieces

- **`swing_screener/lab/`** — a new pure-pandas package (deliberately free of
  pipeline/notify/matplotlib, so the cockpit imports it at module scope):
  - `frames.py` — per-timeframe fetch/resample glue over `data.fetch` +
    `data.resample`, with the USER-TRIGGERED error posture (RuntimeError on an
    empty upstream, the `options.chain` precedent — never a silent None). 4h
    resamples 60 days of 1h bars, so ~60 days is its honest ceiling.
  - `levels.py` — swing pivots (the strict-inequality rule shared with
    `entry_zone.nearest_resistance`, generalised to both sides), greedy price
    clustering, the S/R split by last close, and Fibonacci retracement of the
    window's dominant swing (direction = which extreme printed later). All from
    STANDARD highs/lows — HA smears real extremes.
  - `payload.py` — the wire payload: real + HA candles, EMA/MACD arrays aligned
    1:1 with the candles, computed over the FULL fetch and served as a tail so
    the left edge is warm; EMA warm-up bars serve **null**, never a biased line
    (EMA200 is honestly all-null on 4h/1mo history).
  - `report.py` — the deterministic facts block the Opus prompt consumes.
- **`indicators/trend.py`** grows the full `macd()` (line + signal + hist);
  `macd_histogram` now delegates (lockstep-tested).
- **`notify/analysis.py`** — `_REASONING_EFFORT`/`_REASONING_MAX_TOKENS` extend
  to `xhigh`/`max` (opus-4.8 adaptive-thinking efforts; the old map silently
  DISABLED thinking for unknown keys), plus `analyze_lab_deep`: one Opus call
  over the facts, markdown-constrained to what the cockpit's renderer supports,
  web_search on, usage captured, deterministic-facts fallback on any failure.
  `Settings.lab_reasoning` (SWING_LAB_REASONING) defaults to **max**.
- **`lab_analyses` table** (migration `c1d7f3e9a5b2`) — unlike
  `analysis_requests` (512-char summary + PDF + email, hourly cloud drain), the
  lab stores the FULL markdown note inline and is drained by an **in-process
  cockpit thread** the moment the POST lands: the lab is interactive research
  and must work on a local box with no cloud worker. Consequences, disclosed in
  the UI: the POST needs ANTHROPIC_API_KEY in the cockpit's environment, spend
  is UNCAPPED (est_cost_usd per note is the visibility, the on-demand-path
  precedent), and there is NO requeue pass — a row stuck `running` past 10 min
  means the cockpit died mid-run and the honest copy is "request again".
- **`cockpit/routers/lab.py`** — `GET /api/lab/bars` (read-only on-demand fetch,
  gex 503 posture, no nonce/watermark), `POST /api/lab/analysis` (X-Cockpit,
  nonce bump, daemon-thread drain), `GET /api/lab/analysis`. Seams (`lab_bars`,
  `lab_worker`) ride create_app kwargs so tests never touch yfinance/Anthropic.
  The SSE token grows a `lab` watermark (id | finished_at | started-count).
- **Frontend** — `TickerLabScreen` + `LabChart`: hand-rolled SVG (zero new
  bundle bytes), tokens-only colors, native `<title>` tooltips. The overlay
  toggle chips double as the legend; a series with no data renders a DISABLED
  chip with an honest title, and level lines are filtered to the visible price
  range, never squeezed in by stretching the domain.

## Non-goals

- No intraday quotes (prices stay "as of last close"; 4h is history, not live).
- No screener/pipeline coupling — the lab never writes signals or config.
- No spend cap on the lab's Opus call (the user asked for it; the estimate is
  recorded per note). Revisit if the audit surface wants a ceiling.
- No chart library; no per-request websocket (list poll + SSE wake, the
  AnalysisPanel pattern).
