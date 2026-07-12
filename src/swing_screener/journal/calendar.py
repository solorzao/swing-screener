"""R-native P&L calendar aggregation over the existing paper-trade book.

Pure read-model math: group a cohort's realized R by exit date and by month so a
cockpit calendar can render per-day and per-month P&L cells. R-native only -- the
caller filters by ``account`` first (per-book), so a cell never pools two books.

Reuses ``analytics.performance``: ``_is_closed_filled`` (which rows count) and
``cost_level_for`` (the cohort's honest slippage vintage stamp), so the calendar
never re-derives either rule.
"""

from collections.abc import Iterable
from datetime import date

from swing_screener.analytics.performance import _is_closed_filled, cost_level_for
from swing_screener.db.models import PaperTrade


def pnl_calendar(
    trades: Iterable[PaperTrade], *, month: date | None = None
) -> dict:
    """Group closed-filled ``realized_r`` by exit date and by month.

    Returns ``{"days": {iso: {"r", "n"}}, "months": {"YYYY-MM": {"r", "n"}},
    "cost_level": str | None}``.

    - ``days`` maps each ISO exit date to its summed R and trade count. When
      ``month`` is given, ``days`` is restricted to that calendar month (the day
      component of ``month`` is ignored) -- the grid a caller renders one month at
      a time. When ``month`` is ``None`` every day is included.
    - ``months`` always spans every month in the cohort (the navigation strip /
      roll-up) regardless of ``month``.
    - ``cost_level`` is ``cost_level_for(trades)`` over the whole input cohort --
      a book-level vintage stamp ("net @0.05" vs mixed gross/net), not a per-cell
      value -- so a cell can be labelled net-vs-mixed honestly.

    A closed-filled row with ``exit_date is None`` cannot be keyed by day or month
    and is skipped from both maps (it still participates in ``cost_level_for``,
    which poisons the stamp to ``None``). Open/unfilled rows never count. Pure and
    safe on empty input.
    """
    days: dict[str, dict] = {}
    months: dict[str, dict] = {}
    for t in trades:
        if not _is_closed_filled(t):
            continue
        d = t.exit_date
        r = t.realized_r
        if d is None or r is None:
            continue
        month_key = f"{d.year:04d}-{d.month:02d}"
        _accumulate(months, month_key, r)
        if month is not None and (d.year, d.month) != (month.year, month.month):
            continue
        _accumulate(days, d.isoformat(), r)
    return {"days": days, "months": months, "cost_level": cost_level_for(trades)}


def _accumulate(bucket: dict[str, dict], key: str, r: float) -> None:
    """Fold one trade's R into ``bucket[key]``, seeding an empty ``{r, n}`` cell."""
    cell = bucket.setdefault(key, {"r": 0.0, "n": 0})
    cell["r"] += r
    cell["n"] += 1
