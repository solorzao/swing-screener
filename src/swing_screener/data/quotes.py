"""Latest-close quotes over the bar cache.

A thin wrapper around :func:`swing_screener.data.fetch.fetch_bars` (all I/O
lives in ``fetch_bars``). Consumed by the digest's already-ran filter
(``notify/run.py``), the cockpit's live prices, and the dashboard's candidate
and trade views until it retires. ``latest_closes`` is called as a module
attribute so tests can monkeypatch that seam and avoid the network.
"""

from pathlib import Path

from swing_screener.data import fetch


def latest_close(ticker: str, *, cache_dir: Path) -> float | None:
    """Most recent daily close for a ticker, or None if unavailable.

    Walks back past NaN closes (cached frames from before the download-seam
    dropna can carry NaN holiday rows): the newest FINITE close is the answer;
    an all-NaN column reads as unavailable. NaN must never escape here -- it is
    truthy and every comparison with it is False, so a NaN "price" silently
    defeats the digest's already-ran filter and actionability classification."""
    df = fetch.fetch_bars(ticker, "1d", cache_dir=cache_dir)
    if df is None or df.empty:
        return None
    closes = df["close"].dropna()
    if closes.empty:
        return None
    return float(closes.iloc[-1])


def latest_closes(tickers: list[str], *, cache_dir: Path) -> dict[str, float]:
    """Latest close per ticker, skipping any that fail to fetch."""
    out: dict[str, float] = {}
    for ticker in tickers:
        price = latest_close(ticker, cache_dir=cache_dir)
        if price is not None:
            out[ticker] = price
    return out
