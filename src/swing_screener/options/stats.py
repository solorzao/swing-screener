"""Session-clustered performance stats for the options lab.

Two books, two honesties (docs/modules/gex-lab.md):

  * The GEX paper book (``account="options-lab"``, ``strategy="gex"``) is POOLED
    inference: realized R clustered by SESSION (a trading day), fed to the equity
    book's clustered-bootstrap lower-bound primitive unchanged and wrapped in the
    same ``Stat`` provenance envelope. Sessions are the cluster unit here, not
    tickers -- the lab trades a handful of names intraday, so same-day trades are
    the correlated block. ``_CLUSTER_FLOOR`` stays equity-only; below it the
    primitive falls back to the IID bound flagged thin, exactly as on the equity
    book.

  * The imported Robinhood book (``account="robinhood"``) is a premium-denominated
    DISPLAY, never pooled: plain labeled aggregates over ``premium_pnl`` per
    strategy tag, with no CI machinery and no R unit anywhere near it.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import _Z95, _clustered_ci_low
from swing_screener.cockpit.stats import Stat
from swing_screener.db.models import OptionPaperTrade, OptionSetup

# No cost model in the lab yet: an explicit "priced flat", not an unknown None.
_COST_LEVEL = "0.00"
_CORPUS_ID = "gex-lab-v1"
_FACET = "gex-lab"


def _sessions(trades: Iterable[OptionPaperTrade]) -> dict[str, list[float]]:
    """Realized R keyed by SESSION (``opened_at`` date) -- the cluster shape the
    equity bootstrap primitive consumes. Rows without a realized R or open time
    carry no clusterable result and are skipped."""
    clusters: dict[str, list[float]] = {}
    for t in trades:
        if t.realized_r is None or t.opened_at is None:
            continue
        clusters.setdefault(t.opened_at.date().isoformat(), []).append(float(t.realized_r))
    return clusters


def _stat_from_sessions(clusters: dict[str, list[float]]) -> Stat:
    """Wrap a ``{session: [R, ...]}`` mapping as a session-clustered ``Stat``.

    Reuses ``_clustered_ci_low`` untouched (it consumes any ``{cluster: [R...]}``
    mapping); only the mean/stderr/IID-bound arithmetic is done here, never the
    bootstrap math. ``n_clusters`` is the distinct-session count and
    ``thin_clusters`` is whatever the primitive reports (its IID fallback below the
    distinct-cluster floor)."""
    realized = [r for rs in clusters.values() for r in rs]
    n = len(realized)
    expectancy = sum(realized) / n if n else 0.0
    stderr = statistics.stdev(realized) / (n ** 0.5) if n >= 2 else 0.0
    iid_low = expectancy - _Z95 * stderr
    ci_low, n_clusters, thin = _clustered_ci_low(clusters, iid_low)
    return Stat(
        value=expectancy,
        n=n,
        n_clusters=n_clusters,
        ci_low=ci_low,
        ci_high=expectancy + _Z95 * stderr,
        cost_level=_COST_LEVEL,
        corpus_id=_CORPUS_ID,
        facet=_FACET,
        unit="R",
        thin_clusters=thin,
    )


def lab_summary(
    session: Session, *, account: str, strategy: str = "gex"
) -> dict[str, object]:
    """Session-clustered expectancy over the account+strategy's closed lab trades,
    as a ``Stat``-shaped dict. Only closed trades with a realized R are pooled, so
    imported Robinhood episodes (premium-denominated, ``realized_r`` NULL) never
    enter -- their book is ``robinhood_summary``, not this one."""
    stmt = select(OptionPaperTrade).where(
        OptionPaperTrade.account == account,
        OptionPaperTrade.strategy == strategy,
        OptionPaperTrade.status == "closed",
        OptionPaperTrade.realized_r.is_not(None),
    )
    return _stat_from_sessions(_sessions(session.scalars(stmt))).as_dict()


def by_grade(session: Session, *, account: str) -> list[dict[str, object]]:
    """One session-clustered ``Stat``-shaped dict per checklist grade, joining each
    closed lab trade to its ``OptionSetup`` via ``setup_id``. Imported episodes
    (no setup) fall out of the inner join. Each row carries its ``grade`` label
    alongside the ``Stat`` fields; grades are returned in sorted order."""
    stmt = (
        select(OptionPaperTrade, OptionSetup.grade)
        .join(OptionSetup, OptionPaperTrade.setup_id == OptionSetup.id)
        .where(
            OptionPaperTrade.account == account,
            OptionPaperTrade.status == "closed",
            OptionPaperTrade.realized_r.is_not(None),
        )
    )
    by_grade_sessions: dict[str, dict[str, list[float]]] = {}
    for trade, grade in session.execute(stmt):
        if trade.realized_r is None or trade.opened_at is None:
            continue
        key = trade.opened_at.date().isoformat()
        by_grade_sessions.setdefault(grade, {}).setdefault(key, []).append(
            float(trade.realized_r)
        )
    out: list[dict[str, object]] = []
    for grade in sorted(by_grade_sessions):
        row = _stat_from_sessions(by_grade_sessions[grade]).as_dict()
        row["grade"] = grade
        out.append(row)
    return out


@dataclass
class _RhBook:
    """Mutable accumulator for one Robinhood strategy tag's premium book."""

    n: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    n_open: int = 0


def robinhood_summary(session: Session) -> dict[str, dict[str, object]]:
    """Premium-denominated Robinhood book per strategy tag -- PLAIN labeled values,
    deliberately NOT ``Stat`` dicts.

    Per ``account="robinhood"`` row, aggregated by ``strategy`` ("gex"/"other"):
    ``{n, total_pnl, wins, losses, open}``. ``total_pnl`` sums ``premium_pnl``;
    wins/losses count closed rows by ``premium_pnl`` sign; ``open`` counts open
    rows. The charter (docs/modules/gex-lab.md) calls this book "a labeled display,
    not pooled inference", so there is no CI machinery and no R unit here."""
    stmt = select(OptionPaperTrade).where(OptionPaperTrade.account == "robinhood")
    books: dict[str, _RhBook] = {}
    for t in session.scalars(stmt):
        book = books.setdefault(t.strategy, _RhBook())
        book.n += 1
        if t.status == "open":
            book.n_open += 1
        if t.premium_pnl is not None:
            book.total_pnl += float(t.premium_pnl)
            if t.status == "closed":
                if t.premium_pnl > 0:
                    book.wins += 1
                elif t.premium_pnl < 0:
                    book.losses += 1
    return {
        tag: {
            "n": b.n,
            "total_pnl": b.total_pnl,
            "wins": b.wins,
            "losses": b.losses,
            "open": b.n_open,
        }
        for tag, b in books.items()
    }
