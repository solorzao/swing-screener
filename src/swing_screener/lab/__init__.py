"""Ticker Lab: on-demand per-ticker technical study for the cockpit's TICKER LAB
screen. Pure pandas over the data/ and indicators/ layers -- deliberately free of
pipeline/, notify/ and matplotlib so the cockpit can import it at module scope
(the cockpit-import-hygiene rule the analysis router documents).

The lab is a research surface, not a signal engine: everything it serves is a
deterministic fact (candles, EMAs, MACD, swing-pivot S/R, Fibonacci retracement,
volume) computed from real OHLC -- Heiken Ashi candles are display data; levels
and anchors always come from STANDARD highs/lows (HA smears real extremes).
"""

from swing_screener.lab.frames import LAB_TIMEFRAMES, fetch_lab_frame
from swing_screener.lab.payload import build_lab_payload
from swing_screener.lab.report import lab_facts_text

__all__ = [
    "LAB_TIMEFRAMES",
    "fetch_lab_frame",
    "build_lab_payload",
    "lab_facts_text",
]
