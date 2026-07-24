"""The cross-book Metrics scoreboard: one card per book plus the ONE sanctioned
cross-book aggregate (the real-money combined pool). Plus a ``paper`` card over the
curated intent book (baseline arm / default variant).

Pure, session-taking read (no writes/commits) kept OUT of the router so it is
unit-testable without a TestClient. The router (a separate task) serializes the
dict this returns.

North Star #2 stance held: the only place two books are pooled into one number is
``combined`` = ``manual_equity`` (the ``Trade`` table) + ``live`` (``PaperTrade``
account ``"live"``) -- both real money, both R -- and it is labeled as such. The
firewalled journal books are never pooled. Robinhood is ``$``-only (no stop -> no R)
and enters no R pool.

R math is NEVER re-derived here: every R aggregate flows through
``analytics.performance.summary_from_realized`` (the shared clustered-bootstrap core)
so the scoreboard and the journal/leaderboards can never drift.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import (
    PerformanceSummary,
    _is_closed_filled,
    closed_by_ticker,
    cost_level_for,
    summarize,
    summary_from_realized,
)
from swing_screener.cockpit.common import _finite_or_none
from swing_screener.cockpit.stats import stat_from_summary
from swing_screener.db.models import PaperTrade, Trade
from swing_screener.journal.record import manual_equity_records, robinhood_records
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT


class _RBody(TypedDict):
    """The R-card fields shared by every R book and the combined pool. A precise
    ``TypedDict`` (not ``dict[str, object]``) so ``BookCard(**_r_body(...))`` /
    ``{**_r_body(...)}`` type-check against the exact field types."""

    expectancy: dict[str, object] | None
    win_rate: float
    n_wins: int
    n_losses: int
    n_closed: int
    profit_factor: float | None


@dataclass(frozen=True)
class BookCard:
    """One book's scoreboard tile. ``expectancy`` is a serialized ``Stat`` (dict) for
    an R book with closes, else ``None`` (a ``$``-only or empty book). ``realized_usd``
    / ``equity_r`` are ``None`` where the book has no dollars / no closes."""

    book: str
    unit: str
    expectancy: dict[str, object] | None
    win_rate: float
    n_wins: int
    n_losses: int
    n_closed: int
    profit_factor: float | None
    realized_usd: float | None
    equity_r: list[list[object]] | None

    def as_dict(self) -> dict[str, object]:
        """Explicit wire form (hand-rolled, never ``asdict`` on the wire)."""
        return {
            "book": self.book,
            "unit": self.unit,
            "expectancy": self.expectancy,
            "win_rate": self.win_rate,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "n_closed": self.n_closed,
            "profit_factor": self.profit_factor,
            "realized_usd": self.realized_usd,
            "equity_r": self.equity_r,
        }


def _cutoff(window: str) -> date | None:
    """The close-date floor for ``window`` ("90"/"180"/"365" days back from today),
    or ``None`` for "all" (no cut).

    The undated-drop behaviour matches ``routers/books.py``'s leaderboard window (a
    still-open row drops from any windowed view); the AXIS differs on purpose --
    books.py windows on ``opened_date``, this scoreboard on the CLOSE date, the
    natural axis for a realized-P&L view."""
    if window == "all":
        return None
    return datetime.now(UTC).date() - timedelta(days=int(window))


def _in_window(closed: date | None, cutoff: date | None) -> bool:
    """True iff a close date passes the window. A ``None`` close date (open row) is
    only kept when there is no cut -- a windowed view is by-close-date."""
    if cutoff is None:
        return True
    return closed is not None and closed >= cutoff


def _cumulative_r(pairs: list[tuple[date, float]]) -> list[list[object]] | None:
    """Cumulative realized R by ascending close date as ``[[iso_date, cum_r], ...]``,
    or ``None`` when there are no closes. Local (not ``analytics.equity_curve``, which
    takes ``PaperTrade``) so the manual/combined R pairs feed the identical shape."""
    if not pairs:
        return None
    cum = 0.0
    out: list[list[object]] = []
    for close_date, realized_r in sorted(pairs, key=lambda p: p[0]):
        cum += realized_r
        out.append([close_date.isoformat(), cum])
    return out


def _r_body(
    summary: PerformanceSummary, *, facet: str, cost_level: str | None = None
) -> _RBody:
    """The R-card fields every R book / the combined pool share: the expectancy Stat
    (only when there are closes -- an empty book renders an honest-empty face, not a
    zero), win/W-L counts, and the profit factor (non-finite -> ``None``: JSON has no
    Infinity, and an all-winner PF must not read as a measured number). ``cost_level``
    is the provable slippage stamp of the SCOPED rows (``None`` for a book with no cost
    model or a mixed pool), mirroring the performance panel's ``_stat_dict``."""
    n = summary.n_closed
    expectancy = (
        stat_from_summary(
            summary, cost_level=cost_level, corpus_id=None, facet=facet, unit="R"
        ).as_dict()
        if n > 0
        else None
    )
    return {
        "expectancy": expectancy,
        "win_rate": summary.win_rate,
        "n_wins": summary.n_wins,
        "n_losses": summary.n_losses,
        "n_closed": n,
        "profit_factor": _finite_or_none(summary.profit_factor) if n > 0 else None,
    }


def _window_paper(rows: list[PaperTrade], cutoff: date | None) -> list[PaperTrade]:
    """Window a ``PaperTrade`` book on its CLOSE date (``exit_date``)."""
    return [t for t in rows if _in_window(t.exit_date, cutoff)]


def _paper_pairs(rows: list[PaperTrade]) -> list[tuple[date, float]]:
    """``(exit_date, realized_r)`` for the closed-filled rows -- the equity-R input."""
    return [
        (t.exit_date, t.realized_r)
        for t in rows
        if _is_closed_filled(t) and t.exit_date is not None and t.realized_r is not None
    ]


def build_scoreboard(
    session: Session, *, window: Literal["all", "90", "180", "365"] = "all"
) -> dict[str, object]:
    """The Metrics scoreboard wire dict: ``{"cards": [...], "combined": {...}}``.

    Cards are in the order ``manual_equity``, ``robinhood``, ``live``, ``paper``.
    ``window`` cuts each book's CLOSED rows by close date ("90"/"180"/"365" days back
    from today; "all" = no cut). Pure read -- no writes, no commits.
    """
    cutoff = _cutoff(window)

    # --- manual equity (the whole Trade table; R via the journal read-model) --------
    # R + clustering come from the journal's manual_equity_records so the two never
    # drift: a record's `symbol` is the ticker and `result` is realized R -- None on a
    # still-open trade OR degenerate risk (stop >= entry). KEEP IN SYNC with the R
    # formula at journal/record.py:125-130.
    manual_records = [
        r for r in manual_equity_records(session) if _in_window(r.closed, cutoff)
    ]
    manual_by_ticker: dict[str, list[float]] = {}
    manual_pairs: list[tuple[date, float]] = []
    for r in manual_records:
        if r.result is not None and r.closed is not None:
            manual_by_ticker.setdefault(r.symbol, []).append(r.result)
            manual_pairs.append((r.closed, r.result))
    # $ is not on the read-model, so sum it straight from the Trade rows.
    trades_by_id = {t.id: t for t in session.scalars(select(Trade))}
    manual_usd = sum(_manual_usd(trades_by_id[r.trade_id]) for r in manual_records)
    manual_summary = summary_from_realized(
        manual_by_ticker, n_total=len(manual_records), n_filled=len(manual_records)
    )
    manual_card = BookCard(
        book="manual_equity", unit="R", realized_usd=manual_usd,
        equity_r=_cumulative_r(manual_pairs), **_r_body(manual_summary, facet="manual_equity"),
    )

    # --- robinhood (imported option episodes; $-only, never R, never pooled) ---------
    rh_pnls: list[float] = [
        r.result for r in robinhood_records(session)
        if _in_window(r.closed, cutoff) and r.result is not None
    ]
    rh_wins = sum(1 for p in rh_pnls if p > 0)
    rh_losses = sum(1 for p in rh_pnls if p < 0)
    rh_n = len(rh_pnls)
    robinhood_card = BookCard(
        book="robinhood", unit="$", expectancy=None,
        # denominator is n_closed (every closed episode, incl. $0 scratches), matching
        # the R cards' win_rate = n_wins / n_closed convention so the books read alike.
        win_rate=rh_wins / rh_n if rh_n else 0.0,
        n_wins=rh_wins, n_losses=rh_losses, n_closed=rh_n,
        profit_factor=None,  # $-only: no R profit factor
        realized_usd=sum(rh_pnls),
        equity_r=None,  # $-only: no cumulative-R curve
    )

    # --- live agent (real PaperTrade rows; empty today under the advisor posture) ----
    live_rows = _window_paper(
        list(session.scalars(select(PaperTrade).where(PaperTrade.account == "live"))),
        cutoff,
    )
    live_summary = summarize(live_rows)
    live_by_ticker = closed_by_ticker(live_rows)
    live_pairs = _paper_pairs(live_rows)
    live_card = BookCard(
        book="live", unit="R",
        # live $ P&L via ExecutionLog join deferred; live book empty under advisor posture.
        realized_usd=None,
        equity_r=_cumulative_r(live_pairs),
        **_r_body(live_summary, facet="live", cost_level=cost_level_for(live_rows)),
    )

    # --- paper (curated intent book: baseline arm, default variant) ------------------
    # NO gold gate here: the `paper` adapter (pipeline/execution.py PaperAdapter._open)
    # never stamps would_surface -- every paper row has would_surface=None, so a gold
    # facet_filter would permanently empty the card. It is also redundant: the `paper`
    # account IS the curated intent book by construction, every row already arm=BASELINE
    # / variant=DEFAULT_VARIANT. (The research grid on Mission Control DOES gold-gate --
    # those rows are stamped; the intent book is not.)
    paper_rows = _window_paper(
        list(session.scalars(select(PaperTrade).where(PaperTrade.account == "paper"))),
        cutoff,
    )
    paper_scoped = [
        t for t in paper_rows if t.arm == BASELINE and t.variant == DEFAULT_VARIANT
    ]
    paper_summary = summarize(paper_scoped)
    paper_card = BookCard(
        book="paper", unit="R", realized_usd=None,  # paper book is R-only, no dollars
        equity_r=_cumulative_r(_paper_pairs(paper_scoped)),
        **_r_body(paper_summary, facet="paper", cost_level=cost_level_for(paper_scoped)),
    )

    # --- combined real-money pool = manual_equity + live (both R) --------------------
    combined_by_ticker = {k: list(v) for k, v in manual_by_ticker.items()}
    for ticker, rs in live_by_ticker.items():
        combined_by_ticker.setdefault(ticker, []).extend(rs)
    live_n = sum(len(v) for v in live_by_ticker.values())
    # n_total/n_filled mix the manual closed-record count with the live closed count:
    # coherence-only (n_total >= n_filled >= n_closed), never served -- the scoreboard
    # exposes no fill_rate, and n_closed is re-derived here from combined_by_ticker.
    combined_summary = summary_from_realized(
        combined_by_ticker,
        n_total=len(manual_records) + live_n,
        n_filled=len(manual_records) + live_n,
    )
    combined = {
        "books": ["manual_equity", "live"],
        "unit": "R",
        **_r_body(combined_summary, facet="combined"),
        "realized_usd": manual_usd + (live_card.realized_usd or 0.0),
        "equity_r": _cumulative_r(manual_pairs + live_pairs),
    }

    return {
        "cards": [
            manual_card.as_dict(),
            robinhood_card.as_dict(),
            live_card.as_dict(),
            paper_card.as_dict(),
        ],
        "combined": combined,
    }


def _manual_usd(t: Trade) -> float:
    """A manual-equity trade's realized ``$``. KEEP IN SYNC with the retired-Streamlit
    math at routers/trades.py:640 (``_realized_usd``): an exit-less close falls back to
    the entry price (realized 0.0), never a guess."""
    exit_price = t.exit_price if t.exit_price is not None else t.entry_price
    return (exit_price - t.entry_price) * t.size
