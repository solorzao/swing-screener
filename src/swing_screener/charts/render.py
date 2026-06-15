from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: must be set before pyplot is imported

import mplfinance as mpf  # noqa: E402  (import after backend is set)
import pandas as pd  # noqa: E402

from swing_screener.signals.detect import PullbackContext  # noqa: E402
from swing_screener.signals.entry_zone import EntryZone  # noqa: E402
from swing_screener.signals.reversal import ReversalContext  # noqa: E402


def render_chart(frame: pd.DataFrame, ctx: PullbackContext | ReversalContext,
                 zone: EntryZone, out_path: Path, *, lookback: int = 80) -> Path:
    """Render an annotated Heiken Ashi chart (HA candles + EMA20/50 + entry zone /
    stop / target lines) to a PNG. Returns the path written."""
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
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axlist = mpf.plot(
        plot_df, type="candle", style="charles", volume=False,
        addplot=addplots, hlines=hlines, returnfig=True,
    )
    # Trigger date as a small gray label in the top-left corner (a friendly
    # format, e.g. "trigger Jun 15, 2026") rather than a centered title.
    axlist[0].text(
        0.01, 0.98, f"trigger {ctx.trigger_ts:%b %d, %Y}",
        transform=axlist[0].transAxes, ha="left", va="top",
        fontsize=8, color="#666666",
    )
    fig.savefig(str(out_path), dpi=100, bbox_inches="tight")
    import matplotlib.pyplot as plt  # already safe under Agg

    plt.close(fig)
    return out_path
