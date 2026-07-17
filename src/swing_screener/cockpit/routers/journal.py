"""The cockpit Journal API: P&L calendar, equity+drawdown curve, MAE/MFE excursions,
day-of-week / hold-time / symbol breakdowns, discipline metrics, mistake-cost, the
day-keyed notebook, per-trade tagging, and the unified trade record.

Backend only (Task 10) -- the Journal UI lands after cockpit Phase 3. Contracts, in
lockstep with the rest of the cockpit:

* Every STATISTIC crosses the wire as a full ``Stat`` dict (breakdown buckets, mistake
  rows) built via ``stats.stat_from_summary`` -- the frontend has no bare-float
  renderer. Descriptive summaries that reuse no ``PerformanceSummary`` (excursion
  means/medians, discipline metrics) and the calendar/curve/notes/records are plain
  hand-rolled dicts, with every served float routed through ``_finite_or_none``.
* Reads are 503-friendly (the app's SQLAlchemyError handler); write endpoints (POST
  notes, POST tag) take ``_require_cockpit`` and bump the action nonce AFTER commit.

Firewall: everything is per-book (R-native swing). ``book`` selects one account; the
journal functions each re-apply the closed-filled filter, and cost provenance is
stamped at the BOOK level (``cost_level_for`` over the book cohort) -- conservative
(it can only ever under-claim "net", never over-claim it), so a bucket is never
labelled cleaner than its book.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.analytics.performance import (
    PerformanceSummary,
    cost_level_for,
    equity_curve,
)
from swing_screener.cockpit.common import (
    ActionNonce,
    _finite_or_none,
    _require_cockpit,
    _utc_iso,
)
from swing_screener.cockpit.stats import stat_from_summary
from swing_screener.db.models import JournalNote, PaperTrade
from swing_screener.journal.breakdowns import by_day_of_week, by_hold_time, by_symbol
from swing_screener.journal.calendar import pnl_calendar
from swing_screener.journal.curve import drawdown_series, max_drawdown
from swing_screener.journal.discipline import discipline_report
from swing_screener.journal.excursions import excursion_summary
from swing_screener.journal.mistakes import mistake_cost
from swing_screener.pipeline.arms import BASELINE
from swing_screener.pipeline.variants import DEFAULT_VARIANT
from swing_screener.journal.record import (
    TradeRecord,
    manual_equity_records,
    robinhood_records,
    trade_records,
)
from swing_screener.journal.repo import add_note, add_tag, notes_for_day, tag_trade

# The note's slot in the trading day / the tag vocabulary partition -- constrained at
# the wire so a bad value is FastAPI's 422, never a stored junk string.
_NoteKind = Literal["premarket", "postmarket", "adhoc"]
_TagKind = Literal["setup", "mistake", "context"]


class NoteCreate(BaseModel):
    """POST /api/journal/notes body. ``source`` is NOT a field: a note written through
    the cockpit is the human's, stamped server-side -- a client can never claim
    screener/analyst provenance. ``body`` is bounded here (the column is Text) purely
    as an abuse guard."""

    day: date
    kind: _NoteKind
    body: str = Field(max_length=10_000)
    module: str | None = Field(default=None, max_length=16)


class TagCreate(BaseModel):
    """POST /api/journal/trades/{book}/{trade_id}/tags body. Get-or-creates the tag
    definition, then applies it as ``source="human"`` (the cockpit is the human's
    surface). Bounds mirror the JournalTag columns so Azure SQL never truncates."""

    kind: _TagKind
    name: str = Field(max_length=64)
    description: str = Field(default="", max_length=256)


def build_journal_router(
    *,
    _session: Callable[[], Iterator[Session]],
    action_nonce: ActionNonce,
) -> APIRouter:
    """The Journal endpoints, closed over the session dependency and the post-action
    wake nonce (bumped by the two write actions here)."""
    router = APIRouter()

    def _book_trades(
        session: Session, book: str, scope: str = "baseline"
    ) -> list[PaperTrade]:
        """One book's ``PaperTrade`` cohort, open or closed, at the chosen SCOPE.

        The machine books are an arm x variant experiment GRID: every screened
        candidate books one row per active arm and variant, so the raw account
        pool counts the same underlying fill up to a dozen times and its R sums
        pool the whole tournament -- unit-mixing the North Star bans from
        displays. ``scope="baseline"`` (the default, and the journal's honest
        evaluation slice) filters to arm==BASELINE and variant==DEFAULT_VARIANT
        -- the same one-row-per-candidate slice reflection grades and
        /api/books/open lists. ``scope="grid"`` hands through the full pool for
        deliberate grid-wide inspection; every response carries the scope so
        the display can label it. Each journal function re-applies its own
        closed-filled filter; the per-book firewall is the account scope."""
        stmt = select(PaperTrade).where(PaperTrade.account == book)
        if scope == "baseline":
            stmt = stmt.where(
                PaperTrade.arm == BASELINE,
                PaperTrade.variant == DEFAULT_VARIANT,
            )
        return list(session.scalars(stmt))

    @router.get("/api/journal/calendar")
    def calendar(
        book: str = "research",
        month: str | None = None,
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """R-native P&L calendar for one book. ``month`` (``YYYY-MM``) narrows the day
        grid to that month (a bad format is 422); the month roll-up always spans the
        whole cohort. Cells are plain ``{r, n}`` dicts (r routed through the
        non-finite->null rule); ``cost_level`` is the book's vintage stamp."""
        month_date = _parse_month(month)
        cal = pnl_calendar(_book_trades(session, book, scope), month=month_date)
        return {
            "days": {k: _cell(v) for k, v in cal["days"].items()},
            "months": {k: _cell(v) for k, v in cal["months"].items()},
            "cost_level": cal["cost_level"],
            "scope": scope,
        }

    @router.get("/api/journal/curve")
    def curve(
        book: str = "research",
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The book's realized equity curve, its underwater (drawdown) series, and the
        max drawdown -- all in R. Points are ``[iso_date, value]`` pairs."""
        points = equity_curve(_book_trades(session, book, scope))
        return {
            "curve": [[d.isoformat(), _finite_or_none(r)] for d, r in points],
            "drawdown": [[d.isoformat(), _finite_or_none(dd)]
                         for d, dd in drawdown_series(points)],
            "max_drawdown": _finite_or_none(max_drawdown(points)),
        }

    @router.get("/api/journal/excursions")
    def excursions(
        book: str = "research",
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """MAE/MFE-in-R summary for the book (means + medians over instrumented
        closed-filled trades). Descriptive, not a Stat -- there is no CI machinery
        behind an excursion mean, so it rides as plain floats, never a fabricated
        interval. Uninstrumented / legacy rows are honoured as absent."""
        summ = excursion_summary(_book_trades(session, book, scope))
        return {
            "n": summ["n"],
            "avg_mae_r": _finite_or_none(summ["avg_mae_r"]),
            "avg_mfe_r": _finite_or_none(summ["avg_mfe_r"]),
            "median_mae_r": _finite_or_none(summ["median_mae_r"]),
            "median_mfe_r": _finite_or_none(summ["median_mfe_r"]),
        }

    @router.get("/api/journal/breakdowns")
    def breakdowns(
        book: str = "research",
        by: Literal["dow", "hold", "symbol"] = "dow",
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Day-of-week / hold-time / symbol slices, each bucket a full Stat (rule 1).
        ``by`` is a closed set (anything else is 422). Empty buckets are kept (dow/hold
        always show every label); ``symbol`` shows only tickers that traded."""
        trades = _book_trades(session, book, scope)
        groups: dict[str, PerformanceSummary]
        if by == "dow":
            groups = by_day_of_week(trades)
        elif by == "hold":
            groups = by_hold_time(trades)
        else:
            groups = by_symbol(trades)
        cost = cost_level_for(trades)
        buckets = {
            label: stat_from_summary(
                summary, cost_level=cost, corpus_id=None, facet=book
            ).as_dict()
            for label, summary in groups.items()
        }
        return {"buckets": buckets}

    @router.get("/api/journal/discipline")
    def discipline(
        book: str = "research",
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Swing discipline metrics over the book's existing columns (giveback,
        stop-honored rate, MAE-before-win) with their counts. Descriptive floats
        (None-safe), routed through the non-finite->null rule."""
        rep = discipline_report(_book_trades(session, book, scope))
        return {
            "giveback_r": _opt_finite(rep["giveback_r"]),
            "stop_honored_rate": _opt_finite(rep["stop_honored_rate"]),
            "avg_mae_before_win": _opt_finite(rep["avg_mae_before_win"]),
            "n_closed": rep["n_closed"],
            "n_with_excursion": rep["n_with_excursion"],
            "n_wins": rep["n_wins"],
            "n_stopped": rep["n_stopped"],
            "n_with_exit_reason": rep["n_with_exit_reason"],
        }

    @router.get("/api/journal/mistakes")
    def mistakes(
        book: str = "research",
        scope: Literal["baseline", "grid"] = "baseline",
        session: Session = Depends(_session),
    ) -> list[dict[str, object]]:
        """Per-mistake realized cost, worst-first. ``total_r``/``n`` are the aggregate
        cost + count (plain, like a KPI); the per-trade expectancy rides as a full Stat
        so the edge claim carries its CI. Book-level cost provenance."""
        trades = _book_trades(session, book, scope)
        cost = cost_level_for(trades)
        return [
            {
                "mistake": row["mistake"],
                "n": row["n"],
                "total_r": _finite_or_none(row["total_r"]),
                "stat": stat_from_summary(
                    row["summary"], cost_level=cost, corpus_id=None, facet=book
                ).as_dict(),
            }
            for row in mistake_cost(session, trades)
        ]

    @router.get("/api/journal/notes")
    def get_notes(
        day: date, session: Session = Depends(_session)
    ) -> list[dict[str, object]]:
        """Every notebook entry for ``day`` (``YYYY-MM-DD``; a bad date is 422), in
        insertion order."""
        return [_note_dict(n) for n in notes_for_day(session, day=day)]

    @router.post("/api/journal/notes", dependencies=[Depends(_require_cockpit)])
    def post_note(
        body: NoteCreate, session: Session = Depends(_session)
    ) -> dict[str, object]:
        """Write a human notebook entry (header-guarded). ``source`` is stamped
        ``human`` server-side; ``created_at`` is the server clock (UTC)."""
        note = add_note(
            session, day=body.day, kind=body.kind, body=body.body,
            source="human", module=body.module, created_at=datetime.now(UTC),
        )
        action_nonce.bump()  # post-action wake: add_note has committed
        return _note_dict(note)

    @router.post(
        "/api/journal/trades/{book}/{trade_id}/tags",
        dependencies=[Depends(_require_cockpit)],
    )
    def post_tag(
        book: str, trade_id: int, body: TagCreate,
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Tag a trade as the human (header-guarded). 404 when no such trade lives in
        ``book`` -- the join has no DB FK (read-model boundary), so the endpoint
        verifies membership itself rather than orphaning a tag. Get-or-creates the tag
        definition, then applies it idempotently (``source="human"``)."""
        trade = session.get(PaperTrade, trade_id)
        if trade is None or trade.account != book:
            raise HTTPException(
                status_code=404, detail=f"no trade {trade_id} in book {book!r}")
        tag = add_tag(session, kind=body.kind, name=body.name,
                      description=body.description)
        link = tag_trade(session, trade_id=trade_id, book=book, tag_id=tag.id,
                         source="human", created_at=datetime.now(UTC))
        action_nonce.bump()  # post-action wake: tag_trade has committed
        return {"trade_id": trade_id, "book": book, "tag_id": tag.id,
                "name": tag.name, "source": link.source}

    @router.get("/api/journal/records")
    def records(
        book: str = "research",
        scope: Literal["baseline", "grid"] = "baseline",
        limit: int = Query(default=200, ge=1, le=1000),
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """The book's trades as display records (with tags + theses), NEWEST first,
        paginated. Display only -- never an aggregate. The two PERSONAL books
        dispatch to their own producers (Trade / robinhood OptionPaperTrade) and
        ignore ``scope``; every machine book reads PaperTrade at the chosen scope
        (baseline slice by default -- see ``_book_trades``). ``total`` counts the
        whole cohort so the display can say "newest N of M" instead of silently
        truncating a multi-thousand-row book."""
        if book == "manual_equity":
            recs = manual_equity_records(session)
        elif book == "robinhood":
            recs = robinhood_records(session)
        else:
            recs = trade_records(session, book=book, scope=scope)
        total = len(recs)
        newest_first = list(reversed(recs))[:limit]
        return {
            "records": [_record_dict(rec) for rec in newest_first],
            "total": total,
            "scope": scope,
        }

    return router


def _parse_month(month: str | None) -> date | None:
    """A ``YYYY-MM`` query string as the first of that month, or None. A malformed
    value is a 422 (client error), never a 500."""
    if month is None:
        return None
    try:
        return datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="month must be YYYY-MM") from exc


def _cell(cell: dict) -> dict[str, object]:
    """One calendar cell on the wire: summed R (non-finite->null) + the trade count."""
    return {"r": _finite_or_none(cell["r"]), "n": cell["n"]}


def _opt_finite(value: float | None) -> float | None:
    """A discipline metric that may be None (unmeasured cohort): pass None through,
    else apply the non-finite->null rule."""
    return None if value is None else _finite_or_none(value)


def _note_dict(note: JournalNote) -> dict[str, object]:
    """A note's wire form: dates ISO, ``created_at`` as an unambiguous UTC string."""
    return {
        "id": note.id,
        "day": note.day.isoformat(),
        "kind": note.kind,
        "module": note.module,
        "body": note.body,
        "source": note.source,
        "created_at": _utc_iso(note.created_at),
    }


def _record_dict(rec: TradeRecord) -> dict[str, object]:
    """A TradeRecord's wire form: dates ISO (None while open), R non-finite->null, and
    tags/theses as plain dicts (their display views)."""
    return {
        "trade_id": rec.trade_id,
        "book": rec.book,
        "module": rec.module,
        "symbol": rec.symbol,
        "direction": rec.direction,
        "opened": rec.opened.isoformat() if rec.opened is not None else None,
        "closed": rec.closed.isoformat() if rec.closed is not None else None,
        "unit": rec.unit,
        # wire key stays "r" for FE compat; value is the unit-tagged result
        "r": _finite_or_none(rec.result) if rec.result is not None else None,
        "tags": [{"name": t.name, "kind": t.kind, "source": t.source}
                 for t in rec.tags],
        "theses": [{"event_kind": h.event_kind, "source": h.source, "body": h.body}
                   for h in rec.theses],
    }
