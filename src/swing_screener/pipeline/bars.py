"""Per-bar feature dict shared by the live nightly run and the offline replay.

``_bar_row`` collapses an enriched frame's last bar into the small dict that the
exit machinery (``pipeline.shadow.advance_open`` -> ``signals.exits.evaluate_exit``)
consumes. It lives in this leaf module -- importing only pandas -- so the replay
backtester can reuse it WITHOUT dragging in ``pipeline.run``'s heavy transitive deps
(chart rendering, blob upload, yfinance). The live run and the replay MUST use the
identical builder, so there is exactly one definition, here.
"""

import pandas as pd

# Bar fields the exit machinery reads: low/high/close (stop/target/close checks),
# shaved_head (HA momentum-flip exit), bearish, shaved_bottom + atr (the softening
# gate + the Chandelier runner trail). NOTE: pipeline.exitcheck keeps its OWN, smaller
# _BAR_KEYS for the intraday real-trade path -- they are intentionally distinct.
_BAR_KEYS = ("low", "high", "close", "shaved_head", "bearish", "shaved_bottom", "atr")


def _bar_row(frame: pd.DataFrame) -> dict[str, float | bool]:
    last = frame.iloc[-1]
    row: dict[str, float | bool] = {k: last[k] for k in _BAR_KEYS}
    # body_shrinking: the HA body is smaller than the prior bar's (momentum
    # decelerating) -- an input to the conditional-partial softening gate. The
    # exit machinery reads it off the bar, so it's computed here where the full
    # frame is in hand. False when there's no prior bar to compare against.
    row["body_shrinking"] = bool(
        len(frame) >= 2 and frame["body_frac"].iloc[-1] < frame["body_frac"].iloc[-2]
    )
    return row
