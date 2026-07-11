"""Latest-close quotes over the bar cache.

A thin pure wrapper around :func:`swing_screener.data.fetch.fetch_bars`.
Consumed by the digest's already-ran filter (``notify/run.py``) and the
cockpit's live prices. ``latest_closes`` is called as a module attribute so
tests can monkeypatch that seam and avoid the network.
"""

from pathlib import Path

from swing_screener.data import fetch


def latest_close(ticker: str, *, cache_dir: Path) -> float | None:
    """Most recent daily close for a ticker, or None if unavailable."""
    df = fetch.fetch_bars(ticker, "1d", cache_dir=cache_dir)
    if df is None or df.empty:
        return None
    return float(df["close"].iloc[-1])


def latest_closes(tickers: list[str], *, cache_dir: Path) -> dict[str, float]:
    """Latest close per ticker, skipping any that fail to fetch."""
    out: dict[str, float] = {}
    for ticker in tickers:
        price = latest_close(ticker, cache_dir=cache_dir)
        if price is not None:
            out[ticker] = price
    return out
