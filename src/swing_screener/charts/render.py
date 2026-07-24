from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: must be set before pyplot is imported

import mplfinance as mpf
import pandas as pd

from swing_screener.signals.detect import PullbackContext
from swing_screener.signals.entry_zone import EntryZone
from swing_screener.signals.reversal import ReversalContext


def render_chart(frame: pd.DataFrame, ctx: PullbackContext | ReversalContext,
                 zone: EntryZone, out_path: Path, *, lookback: int = 80) -> Path:
    """Render a Heiken Ashi chart (HA candles + EMA20/50 + entry zone / stop /
    target lines) to a PNG. Returns the path written.

    Deliberately unannotated: no per-chart "Price" label or trigger-date overlay --
    the price ticks are self-evident and the run date lives once in the PDF header.
    A consistent wide aspect ratio (``figratio``) keeps the candles from stretching
    when placed in the PDF. ``ctx`` is accepted for signature stability (callers
    pass it) but not drawn.
    """
    view = frame.tail(lookback)
    plot_df = pd.DataFrame(
        {
            "Open": view["ha_open"], "High": view["ha_high"],
            "Low": view["ha_low"], "Close": view["ha_close"],
        }
    )
    addplots = [
        mpf.make_addplot(view["ema_fast"], color="#1f77b4", width=0.8),
        mpf.make_addplot(view["ema_slow"], color="#ff7f0e", width=0.8),
    ]
    hlines = {
        "hlines": [zone.floor, zone.ceiling, zone.stop, zone.target],
        "colors": ["#2ca02c", "#2ca02c", "#d62728", "#9467bd"],
        "linestyle": "--",
        "linewidths": 0.8,
    }
    # Date format scales with the visible span so labels never repeat: a daily
    # chart (~months) needs the day ("Mar 16"), a weekly (~year+) the month+year
    # ("Mar '26"), a monthly (multi-year) just the year. A single global format
    # produced duplicate "Mar '26 / Mar '26" ticks on the lower timeframes.
    span_days = (view.index[-1] - view.index[0]).days if len(view) > 1 else 0
    if span_days <= 200:
        datefmt = "%b %d"
    elif span_days <= 365 * 3:
        datefmt = "%b '%y"
    else:
        datefmt = "%Y"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, _ = mpf.plot(
        plot_df, type="candle", style="charles", volume=False,
        addplot=addplots, hlines=hlines, returnfig=True,
        ylabel="", figratio=(16, 7), figscale=1.1,
        datetime_format=datefmt, xrotation=0,
    )
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt  # already safe under Agg

    plt.close(fig)
    return out_path
