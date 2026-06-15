"""Tests for the deep-analysis market context layer (yfinance fundamentals + news).

All offline: the raw yfinance calls are injected via the ``fetch`` seam, so no
network is touched. The layer must NEVER raise (per-ticker isolation, like
data.fetch) -- a missing field, an ETF with no fundamentals, or a rate-limit
degrades to None/[] so the Opus analyst still runs and the deterministic
fallback covers the empty case.
"""

from swing_screener.notify.market_context import (
    Fundamentals,
    NewsItem,
    context_block,
    get_fundamentals,
    get_recent_news,
)

# A representative yfinance .info subset. Yahoo returns margins/growth/ROE as
# DECIMAL FRACTIONS (0.27 == 27%); debtToEquity is a PERCENT-style number.
_INFO = {
    "sector": "Technology", "industry": "Consumer Electronics",
    "marketCap": 3_000_000_000_000, "trailingPE": 30.1, "forwardPE": 27.4,
    "profitMargins": 0.27, "grossMargins": 0.45, "revenueGrowth": 0.08,
    "earningsGrowth": 0.11, "debtToEquity": 79.55, "returnOnEquity": 1.5,
    "recommendationKey": "buy", "recommendationMean": 1.9, "numberOfAnalystOpinions": 34,
    "targetMeanPrice": 250.0, "targetLowPrice": 200.0, "targetHighPrice": 300.0,
    "fiftyTwoWeekLow": 164.0, "fiftyTwoWeekHigh": 260.0,
}


def test_get_fundamentals_maps_and_scales_fields():
    f = get_fundamentals("AAPL", fetch=lambda t: dict(_INFO))
    assert f.ok and f.ticker == "AAPL"
    assert f.sector == "Technology" and f.industry == "Consumer Electronics"
    assert f.market_cap == 3_000_000_000_000
    assert f.trailing_pe == 30.1 and f.forward_pe == 27.4
    assert f.profit_margin_pct == 27.0     # 0.27 fraction -> 27.0 percent
    assert f.revenue_growth_pct == 8.0
    assert f.return_on_equity_pct == 150.0  # 1.5 -> 150.0
    assert f.debt_to_equity == 79.55        # left on Yahoo's percent scale
    assert f.recommendation == "buy" and f.num_analysts == 34
    assert f.week52_low == 164.0 and f.week52_high == 260.0


def test_get_fundamentals_is_none_safe_for_etfs():
    # ETFs/funds return None for essentially every equity fundamental.
    f = get_fundamentals("SPY", fetch=lambda t: {})
    assert f.ok is False
    assert f.sector is None and f.market_cap is None and f.trailing_pe is None
    assert f.profit_margin_pct is None and f.recommendation is None


def test_get_fundamentals_never_raises_on_fetch_error():
    def boom(_t):
        raise RuntimeError("rate limited")

    f = get_fundamentals("AAPL", fetch=boom)
    assert isinstance(f, Fundamentals)
    assert f.ok is False and f.sector is None  # degrades, no exception


# Current (nested) yfinance news schema: item = {id, content: {...}}.
_NEWS = [
    {"id": "1", "content": {
        "title": "AAPL hits record", "summary": "Shares jumped.",
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://reuters.com/a"},
        "pubDate": "2026-06-15T14:22:28Z", "contentType": "STORY"}},
    {"id": "2", "content": {
        "title": "Analyst upgrade", "description": "Raised to buy.",
        "provider": {"displayName": "Bloomberg"},
        "clickThroughUrl": {"url": "https://bloomberg.com/b"},
        "pubDate": "2026-06-14T09:00:00Z"}},
]


def test_get_recent_news_parses_nested_schema():
    items = get_recent_news("AAPL", n=5, fetch=lambda t, n: list(_NEWS))
    assert len(items) == 2
    assert items[0].title == "AAPL hits record"
    assert items[0].publisher == "Reuters"
    assert items[0].url == "https://reuters.com/a"
    assert items[0].published_utc.startswith("2026-06-15")
    # second item: link falls back to clickThroughUrl, summary to description
    assert items[1].url == "https://bloomberg.com/b"
    assert items[1].summary == "Raised to buy."


def test_get_recent_news_limits_and_is_none_safe():
    weird = [{"id": "x"}, {"content": {}}, None]  # no content / empty / None
    items = get_recent_news("AAPL", n=2, fetch=lambda t, n: weird)
    assert len(items) == 2  # capped at n
    assert all(isinstance(i, NewsItem) for i in items)


def test_get_recent_news_never_raises_on_fetch_error():
    def boom(_t, _n):
        raise RuntimeError("rate limited")

    assert get_recent_news("AAPL", fetch=boom) == []


def test_context_block_renders_present_fields_and_news():
    f = get_fundamentals("AAPL", fetch=lambda t: dict(_INFO))
    news = get_recent_news("AAPL", fetch=lambda t, n: list(_NEWS))
    block = context_block(f, news)
    assert "Technology" in block and "Consumer Electronics" in block
    assert "27.0%" in block            # profit margin rendered as a percentage
    assert "Reuters" in block and "AAPL hits record" in block


def test_context_block_handles_empty_context():
    f = get_fundamentals("SPY", fetch=lambda t: {})
    block = context_block(f, [])
    assert isinstance(block, str) and block  # no crash, non-empty placeholder text
