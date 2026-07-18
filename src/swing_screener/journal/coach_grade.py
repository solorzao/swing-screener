"""The Personal Trade Coach's deterministic grader -- code owns every number.

``equity_review_facts`` / ``option_review_facts`` stamp the hard facts of one real
manual trade BEFORE any LLM prose. Pure functions over the ORM row (no session), in
the analytics flavour of ``journal.excursions``: bind every nullable to a local, guard
it, and return an honest ``None`` where a fact is unmeasurable rather than a fake zero.

Deliberately NOT computed: MAE/MFE excursion -- the ``Trade`` table records no
low/high water marks (only entry/stop/target/exit), so the honest value is ``None``
until a hold-period bar-window backfill is added (design SS Scope, deferred). The
``robinhood`` book is premium-denominated ($), never R, and carries no A+ checklist
grade (imported episodes have ``setup_id=None``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from swing_screener.db.models import OptionPaperTrade, Trade


@dataclass(frozen=True)
class TradeReviewFacts:
    """The code-owned scorecard for one reviewed trade. ``unit`` interprets ``result``
    ("R" for equity, "$" for options). ``mae_r``/``mfe_r`` are always None in v2."""

    book: str
    symbol: str
    unit: str
    result: float | None            # R (equity) or premium $ (options); None if unmeasurable
    outcome: str                    # target | stop | other | open
    hold_days: int | None
    moved_stop: bool                # equity discipline flag (override signals a stop move)
    override: str | None            # equity only
    emotional_state: str | None     # equity only
    exit_reason: str | None
    mae_r: float | None = None      # not computable in v2 (no water marks)
    mfe_r: float | None = None


def _outcome(exit_reason: str | None, *, closed: bool) -> str:
    if not closed:
        return "open"
    if exit_reason in ("target", "stop"):
        return exit_reason
    return "other"


def equity_review_facts(t: Trade) -> TradeReviewFacts:
    """Facts for one real manual EQUITY trade (``Trade``). R is computed
    ``(exit-entry)/(entry-stop)`` with the close endpoint's guard: ``risk <= 0`` or
    still open -> ``None`` (never a bogus R)."""
    entry = t.entry_price
    stop = t.stop
    exit_price = t.exit_price
    closed = exit_price is not None
    risk = entry - stop
    result = (exit_price - entry) / risk if (exit_price is not None and risk > 0) else None

    hold_days = (t.exit_date - t.entry_date).days if t.exit_date is not None else None
    override = t.override
    moved_stop = override is not None and "stop" in override.lower()

    return TradeReviewFacts(
        book="manual_equity",
        symbol=t.ticker,
        unit="R",
        result=result,
        outcome=_outcome(t.exit_reason, closed=closed),
        hold_days=hold_days,
        moved_stop=moved_stop,
        override=override,
        emotional_state=t.emotional_state,
        exit_reason=t.exit_reason,
    )


def option_review_facts(t: OptionPaperTrade) -> TradeReviewFacts:
    """Facts for one real imported Robinhood OPTION episode. Premium P&L in $, hold
    time, exit reason -- no R, no A+ grade (design SS Boundary).

    NOTE: not wired -- the coach runner never selects robinhood episodes, so no
    robinhood coach review has ever been produced. Kept deliberately (2026-07-17
    audit decision #3): wiring robinhood reviews is a pending product decision,
    and this is the ready-made facts stamper for it. Covered by its own test."""
    opened = t.opened_at
    closed_at = t.closed_at
    hold_days = (closed_at - opened).days if (opened is not None and closed_at is not None) else None

    return TradeReviewFacts(
        book="robinhood",
        symbol=t.underlying,
        unit="$",
        result=t.premium_pnl,
        outcome=_outcome(t.exit_reason, closed=closed_at is not None),
        hold_days=hold_days,
        moved_stop=False,           # N/A for the imported options book
        override=None,
        emotional_state=None,
        exit_reason=t.exit_reason,
    )


def facts_dict(facts: TradeReviewFacts) -> dict[str, object]:
    """JSON-ready dict for ``journal_reviews.facts_json`` (the authoritative scorecard)."""
    return asdict(facts)
