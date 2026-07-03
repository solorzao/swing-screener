"""Digest selection queries over the SQLite store.

The digest orchestrator uses these to pick which signals appear in each
cadence (daily / weekly / monthly) and which exit alerts to send.
"""

from datetime import date, timedelta

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal, Universe
from swing_screener.pipeline.diversity import cap_by_sector


def _fresh_enough(session: Session, run_date: date,
                  max_age_days: int | None) -> list[ColumnElement[bool]]:
    """A staleness/cooldown WHERE clause (as a list to splat into ``.where``).

    When ``max_age_days`` is set, keep only setups first seen within the last
    ``max_age_days`` SCREEN RUNS -- counted over the distinct ``run_date``s actually in
    the signals table (the trading calendar the system experienced), NOT calendar days.
    Calendar arithmetic aged weekend-spanning setups out one appearance early: with
    cooldown=1, Monday - 1 day = Sunday, so every Friday-fresh pick was dropped from
    Monday's digest (2026-07 audit). A pick whose ``first_seen_date`` predates the
    cutoff run has been on the list too long and is dropped, so the same play isn't
    re-pitched run after run. Legacy rows (NULL ``first_seen_date``) are never dropped
    (fail-open); fewer prior runs than the window (a fresh store) falls back to the
    oldest run available. ``None`` disables the cooldown entirely.
    """
    if max_age_days is None:
        return []
    recent_runs = list(session.scalars(
        select(Signal.run_date).where(Signal.run_date <= run_date)
        .distinct().order_by(Signal.run_date.desc()).limit(max_age_days + 1)
    ))
    cutoff = recent_runs[-1] if recent_runs else run_date - timedelta(days=max_age_days)
    return [or_(Signal.first_seen_date.is_(None), Signal.first_seen_date >= cutoff)]


def daily_picks(session: Session, run_date: date, *, top_n: int = 5,
                max_age_days: int | None = None,
                max_per_sector: int | None = None) -> list[Signal]:
    """Top-N CONTINUATION signals overall for the run date (any timeframe).

    The daily digest is the day's best continuation picks across all timeframes;
    weekly_picks/monthly_picks are the timeframe-specific cadences, and
    reversal_picks is the separate oversold-bounce list. ``max_age_days`` applies the
    staleness cooldown (see ``_fresh_enough``).

    ``max_per_sector`` (when set) caps how many picks may share a GICS sector (joined
    from ``Universe.sector``), promoting lower-ranked picks from other sectors so one hot
    sector can't fill the list. Picks whose ticker has no sector are never capped
    (fail-open). None leaves the result a pure rank-ordered top-N.
    """
    where = (Signal.run_date == run_date, Signal.play_type == "continuation",
             *_fresh_enough(session, run_date, max_age_days))
    if max_per_sector is None:
        stmt = select(Signal).where(*where).order_by(Signal.rank).limit(top_n)
        return list(session.scalars(stmt))
    # Join each candidate to its sector and cap in rank order (no SQL LIMIT: the cap may
    # need to reach past top_n to fill the list once a sector saturates).
    joined = (
        select(Signal, Universe.sector)
        .join(Universe, Universe.ticker == Signal.ticker, isouter=True)
        .where(*where)
        .order_by(Signal.rank)
    )
    rows = session.execute(joined).all()
    capped = cap_by_sector(rows, lambda r: r[1], max_per_sector=max_per_sector, limit=top_n)
    return [r[0] for r in capped]


def reversal_picks(session: Session, run_date: date, *, top_n: int = 5,
                   max_age_days: int | None = None,
                   confirmed_only: bool = False, premium_only: bool = False) -> list[Signal]:
    """Top-N REVERSAL signals overall for the run date (any timeframe) -- the
    oversold-bounce / relief-rally list, ranked best first.

    ``premium_only`` keeps only the PREMIUM conviction tier (high-volume bounce AND Wyckoff
    spring -- the additive +0.25R edge). ``confirmed_only`` keeps CONFIRMED-strength reversals.
    The two COMPOSE (AND) when both are set -- premium used to silently override
    confirmed_only via an elif, which is how the 2026-06-28 premium-only regression blanked
    the digest. Either way the filtered-out reversals are still stored/shadow-tracked, just
    hidden from the digest (the learning loop is preserved)."""
    where = [Signal.run_date == run_date, Signal.play_type == "reversal",
             *_fresh_enough(session, run_date, max_age_days)]
    if premium_only:
        where.append(Signal.conviction_tier == "premium")
    if confirmed_only:
        where.append(Signal.strength == "confirmed")
    stmt = select(Signal).where(*where).order_by(Signal.rank).limit(top_n)
    return list(session.scalars(stmt))


def cap_signals_by_sector(session: Session, signals: list[Signal], *,
                          max_per_sector: int | None, limit: int) -> list[Signal]:
    """Cap an already-ranked signal list to ``max_per_sector`` per GICS sector, then trim
    to ``limit`` -- the reversal-list counterpart of ``daily_picks``' cap (2026-07-02: 31
    same-day confirmations crowded every software rotation name out of the top-5).

    Runs over an in-memory list (the caller filters staleness/actionability first, so a
    dropped pick never consumes a sector slot). Unknown sectors are never capped
    (fail-open); ``max_per_sector=None`` just trims to ``limit``."""
    if max_per_sector is None:
        return signals[:limit]
    tickers = {s.ticker for s in signals}
    rows = session.execute(
        select(Universe.ticker, Universe.sector).where(Universe.ticker.in_(tickers))
    ).all()
    sector_of = {t: sec for t, sec in rows}
    capped = cap_by_sector(signals, lambda s: sector_of.get(s.ticker),
                           max_per_sector=max_per_sector, limit=limit)
    return list(capped)


def reversal_funnel(session: Session, run_date: date) -> tuple[int, int]:
    """``(detected, confirmed)`` counts of ALL reversal signals stored for the run date.

    Deliberately unfiltered (no cooldown, no tier/strength gate): these counts let the
    digest say "N detected, M confirmed, K surfaced" so a day where the surfacing bar
    filtered everything out is visibly different from a day where nothing fired -- the
    2026-06-28 premium-only regression was invisible for days precisely because the
    email rendered the same empty state for both.
    """
    rows = session.execute(
        select(Signal.strength, func.count())
        .where(Signal.run_date == run_date, Signal.play_type == "reversal")
        .group_by(Signal.strength)
    ).all()
    detected = sum(n for _, n in rows)
    confirmed = sum(n for strength, n in rows if strength == "confirmed")
    return detected, confirmed


def _by_timeframe(session: Session, run_date: date, timeframe: str, top_n: int,
                  max_age_days: int | None) -> list[Signal]:
    stmt = (
        select(Signal)
        .where(Signal.run_date == run_date, Signal.timeframe == timeframe,
               Signal.play_type == "continuation",
               *_fresh_enough(session, run_date, max_age_days))
        .order_by(Signal.rank)
        .limit(top_n)
    )
    return list(session.scalars(stmt))


def weekly_picks(session: Session, run_date: date, *, top_n: int = 5,
                 max_age_days: int | None = None) -> list[Signal]:
    """Top-N weekly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1wk", top_n, max_age_days)


def monthly_picks(session: Session, run_date: date, *, top_n: int = 5,
                  max_age_days: int | None = None) -> list[Signal]:
    """Top-N monthly-timeframe signals for the run date."""
    return _by_timeframe(session, run_date, "1mo", top_n, max_age_days)


def pending_exit_alerts(session: Session, run_date: date) -> list[ExitEvent]:
    """Exit events for REAL trades on the given date (paper-trade events excluded)."""
    # `== False` renders `is_paper = 0`; `.is_(False)` renders `IS 0`, which is a
    # syntax error on SQL Server (valid only on SQLite).
    stmt = (
        select(ExitEvent)
        .where(ExitEvent.created_date == run_date, ExitEvent.is_paper == False)  # noqa: E712
        .order_by(ExitEvent.id.desc())
    )
    return list(session.scalars(stmt))
