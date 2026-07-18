"""Per-ticker market context for the deep-analysis digest path.

Company fundamentals and recent news pulled from yfinance, fed as ground-truth
context to the Opus analyst (alongside the chart image and the deterministic
facts). Both are best-effort and NEVER raise -- mirroring ``data.fetch``'s
per-ticker isolation: a missing field, an ETF with no fundamentals, a rate-limit,
or any scrape failure degrades to ``None``/``[]`` so the analyst still runs with
whatever is available, and the digest's deterministic fallback covers the case
where there is nothing at all.

The raw yfinance calls are isolated in thin, mockable seams (``_yf_info`` /
``_yf_news``) so tests never touch the network. yfinance returns margins/growth/
ROE as DECIMAL FRACTIONS (0.27 == 27%); ``debtToEquity`` is a PERCENT-style
number (79.5 means D/E ~0.80). Yahoo pads missing fundamentals with NaN as
readily as None (PR #104's class) -- every numeric field is normalized through
``_num`` at ingestion so a non-finite value gets the same missing treatment and
``context_block`` can never feed the analyst a literal 'nan' fact. The news
payload uses the current NESTED schema (``item["content"][...]``), not the
legacy flat fields.
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


def _yf_info(ticker: str) -> dict:
    """Thin, mockable wrapper around yfinance ``.info`` (the fundamentals call)."""
    import yfinance as yf

    return yf.Ticker(ticker).info or {}


def _yf_news(ticker: str, n: int) -> list[dict]:
    """Thin, mockable wrapper around yfinance news (current nested-content schema)."""
    import yfinance as yf

    return yf.Ticker(ticker).get_news(count=max(n, 1), tab="news") or []


def _num(x: object) -> float | None:
    """A finite float from a raw yfinance value, else None (rejects bools).

    NaN/inf get the same treatment as a missing field -- fail-safe, so the
    ``is not None`` guards in ``context_block`` are sufficient to keep 'nan'
    out of the analyst prompt."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _pct(x: object) -> float | None:
    """Yahoo fraction -> percent (0.27 -> 27.0). None/NaN-safe; rejects bools."""
    n = _num(x)
    return round(n * 100, 2) if n is not None else None


@dataclass(frozen=True)
class Fundamentals:
    """Curated, None-safe fundamentals snapshot. ``ok`` is False when the fetch
    failed or returned nothing usable (e.g. an ETF, an uncovered name)."""

    ticker: str
    ok: bool
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    profit_margin_pct: float | None = None
    gross_margin_pct: float | None = None
    revenue_growth_pct: float | None = None
    earnings_growth_pct: float | None = None
    debt_to_equity: float | None = None  # Yahoo percent scale (~79.5 == D/E 0.80)
    return_on_equity_pct: float | None = None
    recommendation: str | None = None
    recommendation_mean: float | None = None
    num_analysts: int | None = None
    target_mean_price: float | None = None
    target_low_price: float | None = None
    target_high_price: float | None = None
    week52_low: float | None = None
    week52_high: float | None = None


@dataclass(frozen=True)
class NewsItem:
    title: str | None = None
    publisher: str | None = None
    summary: str | None = None
    url: str | None = None
    published_utc: str | None = None


def get_fundamentals(
    ticker: str, *, fetch: Callable[[str], dict] = _yf_info
) -> Fundamentals:
    """Fetch + curate fundamentals for ``ticker``. Never raises."""
    try:
        info = fetch(ticker) or {}
    except Exception:  # noqa: BLE001 -- isolation: any scrape/network/rate-limit failure
        log.warning("fundamentals fetch failed for %s; continuing without", ticker)
        return Fundamentals(ticker=ticker.upper(), ok=False)

    g = info.get
    market_cap = _num(g("marketCap"))
    num_analysts = _num(g("numberOfAnalystOpinions"))
    return Fundamentals(
        ticker=ticker.upper(),
        ok=g("sector") is not None or market_cap is not None,
        sector=g("sector"),
        industry=g("industry"),
        market_cap=market_cap,
        trailing_pe=_num(g("trailingPE")),
        forward_pe=_num(g("forwardPE")),
        profit_margin_pct=_pct(g("profitMargins")),
        gross_margin_pct=_pct(g("grossMargins")),
        revenue_growth_pct=_pct(g("revenueGrowth")),
        earnings_growth_pct=_pct(g("earningsGrowth")),
        debt_to_equity=_num(g("debtToEquity")),
        return_on_equity_pct=_pct(g("returnOnEquity")),
        recommendation=g("recommendationKey"),
        recommendation_mean=_num(g("recommendationMean")),
        num_analysts=int(num_analysts) if num_analysts is not None else None,
        target_mean_price=_num(g("targetMeanPrice")),
        target_low_price=_num(g("targetLowPrice")),
        target_high_price=_num(g("targetHighPrice")),
        week52_low=_num(g("fiftyTwoWeekLow")),
        week52_high=_num(g("fiftyTwoWeekHigh")),
    )


def get_recent_news(
    ticker: str, n: int = 5, *, fetch: Callable[[str, int], list[dict]] = _yf_news
) -> list[NewsItem]:
    """Fetch up to ``n`` recent news items for ``ticker``. Never raises."""
    try:
        raw = fetch(ticker, n) or []
    except Exception:  # noqa: BLE001 -- isolation (see get_fundamentals)
        log.warning("news fetch failed for %s; continuing without", ticker)
        return []

    items: list[NewsItem] = []
    for art in raw[:n]:
        c = art.get("content") or {} if isinstance(art, dict) else {}
        url = (c.get("canonicalUrl") or {}).get("url") or (
            c.get("clickThroughUrl") or {}
        ).get("url")
        pub = c.get("pubDate")
        items.append(
            NewsItem(
                title=c.get("title"),
                publisher=(c.get("provider") or {}).get("displayName"),
                summary=c.get("summary") or c.get("description"),
                url=url,
                published_utc=pub if isinstance(pub, str) else None,
            )
        )
    return items


def context_block(fundamentals: Fundamentals, news: list[NewsItem]) -> str:
    """Render fundamentals + news into a compact text block for the LLM prompt.

    None fields are omitted; an empty snapshot yields an explicit "not available"
    line so the model knows the data is absent rather than silently missing.
    """
    f = fundamentals
    rows: list[str] = []
    if f.sector or f.industry:
        rows.append(f"Sector / industry: {f.sector or '?'} / {f.industry or '?'}")
    if f.market_cap is not None:
        rows.append(f"Market cap: {f.market_cap:,.0f}")
    pe = []
    if f.trailing_pe is not None:
        pe.append(f"TTM {f.trailing_pe:.1f}")
    if f.forward_pe is not None:
        pe.append(f"fwd {f.forward_pe:.1f}")
    if pe:
        rows.append("P/E: " + ", ".join(pe))
    if f.profit_margin_pct is not None:
        rows.append(f"Profit margin: {f.profit_margin_pct:.1f}%")
    if f.gross_margin_pct is not None:
        rows.append(f"Gross margin: {f.gross_margin_pct:.1f}%")
    if f.revenue_growth_pct is not None:
        rows.append(f"Revenue growth (yoy): {f.revenue_growth_pct:.1f}%")
    if f.earnings_growth_pct is not None:
        rows.append(f"Earnings growth (yoy): {f.earnings_growth_pct:.1f}%")
    if f.return_on_equity_pct is not None:
        rows.append(f"Return on equity: {f.return_on_equity_pct:.1f}%")
    if f.debt_to_equity is not None:
        rows.append(f"Debt/equity: ~{f.debt_to_equity / 100:.2f}x")
    if f.recommendation:
        rec = f"Analyst consensus: {f.recommendation}"
        if f.recommendation_mean is not None:
            rec += f" (mean {f.recommendation_mean:.1f}/5)"
        if f.num_analysts:
            rec += f", {f.num_analysts} analysts"
        rows.append(rec)
    if f.target_mean_price is not None:
        tgt = f"Mean price target: {f.target_mean_price:.2f}"
        if f.target_low_price is not None and f.target_high_price is not None:
            tgt += f" (range {f.target_low_price:.2f}-{f.target_high_price:.2f})"
        rows.append(tgt)
    if f.week52_low is not None and f.week52_high is not None:
        rows.append(f"52-week range: {f.week52_low:.2f} - {f.week52_high:.2f}")

    fund_text = (
        "Fundamentals:\n" + "\n".join(f"- {r}" for r in rows)
        if rows
        else "Fundamentals: not available."
    )

    news_rows: list[str] = []
    for it in news:
        if not it.title:
            continue
        meta = " / ".join(x for x in (it.publisher, it.published_utc) if x)
        row = f"- {it.title}" + (f" [{meta}]" if meta else "")
        if it.summary:
            row += f": {it.summary}"
        news_rows.append(row)
    news_text = "Recent news:\n" + "\n".join(news_rows) if news_rows else "Recent news: none found."

    return f"{fund_text}\n\n{news_text}"
