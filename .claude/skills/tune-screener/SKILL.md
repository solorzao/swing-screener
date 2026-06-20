---
name: tune-screener
description: >-
  Run the swing-screener config optimizer and explain, in plain language, whether the live
  freshness gate (max_extension_atr) should change — the easy way to use the
  build → measure → optimize loop. Use when the user says "tune the screener", "should I change
  the config", "run the optimizer", "is there a better gate", or wants a screener-tuning
  recommendation. Optionally takes a comma-separated ticker list as the argument.
---

# Tune the screener (config optimizer)

Run the walk-forward optimizer, read its leaderboard, and tell the user — without jargon —
whether there's a config change worth making. **Never edit config or open a PR without
confirming first.** The whole point is a vetted recommendation the human approves.

## Steps

1. **Pick the interpreter.** Use `.venv/bin/python` if it exists, otherwise `python` (the
   project needs Python 3.12 with `pip install -e ".[dev]"`). If imports fail, set that up first.

2. **Pick tickers.** If the user passed a comma-separated list as the argument, use it.
   Otherwise default to a liquid basket:
   `AMD,NVDA,AAPL,MSFT,META,AMZN,GOOGL,TSLA,JPM,XOM,WMT,AVGO`.

3. **Run the optimizer** (it fetches the daily history itself, cached under `.cache`):

   ```
   <python> -m swing_screener.pipeline.optimize --tickers <TICKERS>
   ```

   It prints an **in-sample leaderboard**, the chosen **winner**, and the winner's
   **out-of-sample** line + a verdict. (If it can't fetch data — e.g. offline — say so and stop.)

4. **Interpret it for the user.** Read the table and explain:
   - Which `ext_*` gate (that's `max_extension_atr`) leads, and by how much expectancy.
   - The decisive question: does the winner **hold out-of-sample** on a **trusted** sample?
     A row marked `thin` (fewer than 20 closed trades) is NOT trustworthy — say so plainly.
   - The shipped/live gate is `ext_2.0`. A change is only worth it if a *different* gate beats
     it out-of-sample on a non-thin sample with a positive lower bound.

5. **Make a recommendation, honestly:**
   - **If there's a trusted, out-of-sample winner that beats `ext_2.0`:** name the proposed value
     (e.g. "tighten the gate from 2.0 to 1.5"), summarize the evidence, and **offer** to open a
     config-change PR. Only on a yes, run:
     ```
     <python> -m swing_screener.pipeline.propose --tickers <TICKERS>
     ```
     then commit the `config.py` edit on a branch and open the PR (this mirrors the weekly
     `optimize.yml` workflow). Let the user review and merge — do not merge for them.
   - **If everything is thin or nothing beats `ext_2.0` out-of-sample:** say there isn't enough
     evidence to change anything yet — the current gate stands — and that the live shadow book
     needs more trades to accumulate. This is the common, correct outcome on a small basket.

## Notes

- Be conservative: under-recommending beats shipping an overfit gate against real money. The
  out-of-sample + non-thin checks are deliberate; respect them.
- For the *live* picture (what the deployed screener's shadow book is actually finding across
  variants and market regimes), point the user at the dashboard's **Screener Performance** page
  (strategy leaderboard, score calibration, regime cut) rather than this offline sweep.
