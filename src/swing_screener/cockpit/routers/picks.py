"""The Candidates / Zone D surface: ``GET /api/picks`` -- the digest's pick set,
recomputed with the digest's OWN call sites so the screen and the email can never
disagree (Phase 3 plan, scope decision 7: show-and-flag, never widen).

Parity is BY CONSTRUCTION, not by copied output: the endpoint calls the same
``notify.select`` pickers with the same StrategyConfig-read knobs in the same
order the digest does (``notify/run.py``) -- daily: ``daily_picks`` (sector cap
INSIDE, top-5) then the liveness drop, NO backfill; reversal: ``reversal_picks``
over the ``REVERSAL_POOL_N`` pool, liveness drop BEFORE the sector cap so dropped
picks free their top-5 slots for backfill (the Jul-2 rotation lesson). Every knob
is read from ``StrategyConfig()`` per request -- including
``digest_drop_already_ran``, which gates the digest's drop in ``notify.run.main``
-- so a config change moves both surfaces together, today's defaults nowhere
baked in.

Liveness-dropped picks ADDITIONALLY ride as flagged extras: they were dropped
before (reversal) or after (daily) the cap, so they never consumed a slot -- the
surfaced lists always match the email.
"""

from collections.abc import Callable, Iterator
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.cockpit.livedata import QuoteCache
from swing_screener.config import StrategyConfig
from swing_screener.db.models import AnalystCall, Signal
from swing_screener.db.repo import latest_run_date, load_scored_analyst_calls
from swing_screener.notify import select as sel
from swing_screener.pipeline.reflect import analyst_calibration
from swing_screener.signals.actionability import classify


def build_picks_router(
    *,
    _session: Callable[[], Iterator[Session]],
    quote_cache: QuoteCache,
) -> APIRouter:
    """The picks endpoint, closed over the app's seams: the session dependency and
    the TTL quote cache (the same instance parked on ``app.state.quote_cache``)."""
    router = APIRouter()

    @router.get("/api/picks")
    def picks(session: Session = Depends(_session)) -> dict[str, object]:
        """The digest's surfaced picks for the latest run, plus the flagged extras.

        The module docstring carries the parity contract (digest call sites, digest
        order, config-read knobs). Wire shape:

        * ``daily`` / ``reversal``: the digest's two top-5 lists, in digest order.
        * ``extras``: the liveness-DROPPED picks (daily-dropped first, then
          reversal-dropped, each in its list's order), same row shape -- the UI
          flags them visually; they never consumed a cap slot.
        * Per row: the LevelRail fields (floor/ceiling/stop/target + ``last_close``
          as the current mark, null on a quote miss), ``actionability``
          {status, dist_r} at the CACHED quote (a miss reads ``unknown`` and the
          pick is kept -- the digest's fail-open posture), ``conviction_tier``,
          ``is_repeat`` (first seen on an earlier run), ``has_chart`` (the chart
          proxy 404s are normal -- most signals are chartless), the cohort ref
          ``(play_type, strength)`` for the client-side cohorts join, and
          ``analyst``: today's AnalystCall grade for the pick's
          (ticker, timeframe, play_type) with that grade's scored record
          ``(n, mean_r)`` from the SAME ``analyst_calibration`` the reflection
          uses -- n=0/mean null when the grade has no scored history ("unproven"),
          null when no call exists for the pick.
        * ``extended`` is NORMAL for a reversal (a resting limit sits above its
          ceiling by definition) -- it stays surfaced; only ``broken`` drops it.
          Continuation drops on both (the anti-chase rule).
        * ``quotes_as_of`` timestamps the quote window's latest contribution.

        Budget: two picker SELECTs, one ``QuoteCache.get`` for all tickers, one
        AnalystCall SELECT, one scored-calls SELECT per play type present.
        """
        scfg = StrategyConfig()
        run_d = latest_run_date(session)
        if run_d is None:  # no screen run yet: an empty surface, not an error
            return {
                "run_date": None, "daily": [], "reversal": [], "extras": [],
                "quotes_as_of": quote_cache.get([]).as_of.isoformat(),
            }

        cooldown = scfg.digest_repeat_cooldown_days
        daily_five = sel.daily_picks(
            session, run_d, max_age_days=cooldown,
            max_per_sector=scfg.daily_max_per_sector)
        rev_pool = sel.reversal_picks(
            session, run_d, top_n=sel.REVERSAL_POOL_N, max_age_days=cooldown,
            premium_only=scfg.reversal_surface_premium_only,
            confirmed_only=scfg.reversal_surface_confirmed_only)

        quote_result = quote_cache.get(
            [s.ticker for s in daily_five + rev_pool])
        prices = quote_result.prices

        if scfg.digest_drop_already_ran:  # the digest's gate (notify.run.main)
            daily, daily_dropped = _split_by_liveness(daily_five, prices)
            rev_kept, rev_dropped = _split_by_liveness(rev_pool, prices)
        else:  # drop disabled -> the digest keeps everything; so do we
            daily, daily_dropped = daily_five, []
            rev_kept, rev_dropped = rev_pool, []
        reversal = sel.cap_signals_by_sector(
            session, rev_kept, max_per_sector=scfg.reversal_max_per_sector,
            limit=5)
        extras = daily_dropped + rev_dropped

        calls = _todays_calls(
            session, run_d,
            [s.ticker for s in daily + reversal + extras])
        calib_cache: dict[str, dict] = {}

        def _row(s: Signal) -> dict[str, object]:
            return _pick_row(
                s, prices.get(s.ticker), run_d=run_d,
                analyst=_analyst_block(session, s, calls, calib_cache))

        return {
            "run_date": run_d.isoformat(),
            "daily": [_row(s) for s in daily],
            "reversal": [_row(s) for s in reversal],
            "extras": [_row(s) for s in extras],
            "quotes_as_of": quote_result.as_of.isoformat(),
        }

    return router


def _split_by_liveness(
    signals: list[Signal], prices: dict[str, float]
) -> tuple[list[Signal], list[Signal]]:
    """``(kept, dropped)`` under the digest's play-type-aware keep rule, RESTATED
    from ``notify.run._drop_already_ran`` rather than imported (its module drags
    the whole notify/pipeline graph -- the ``analysis.py`` ``_STALE_AFTER``
    precedent; the parity test computes its expectation THROUGH the real function,
    so the two cannot drift silently). Continuation drops on ``extended`` (the
    chase) and ``broken`` (stop violated); a reversal is a resting limit whose
    normal state is above its ceiling, so ``extended`` is kept and only ``broken``
    drops it. No quote -> ``unknown`` -> kept (fail-open, like the digest)."""
    kept: list[Signal] = []
    dropped: list[Signal] = []
    for s in signals:
        status = classify(entry_floor=s.entry_floor, entry_ceiling=s.entry_ceiling,
                          stop=s.stop, price=prices.get(s.ticker)).status
        keep = ("actionable", "unknown", "extended") if s.play_type == "reversal" \
            else ("actionable", "unknown")
        (kept if status in keep else dropped).append(s)
    return kept, dropped


def _todays_calls(
    session: Session, run_d: date, tickers: list[str]
) -> dict[tuple[str, str, str], AnalystCall]:
    """Today's AnalystCalls keyed by the pick identity (ticker, timeframe,
    play_type) -- ONE query for every rendered row, so the endpoint never goes
    N+1 over the pick set. Ascending id, so a re-recorded call (a forced digest
    resend) wins with the newest row."""
    if not tickers:
        return {}
    rows = session.scalars(
        select(AnalystCall)
        .where(AnalystCall.run_date == run_d, AnalystCall.ticker.in_(set(tickers)))
        .order_by(AnalystCall.id)
    )
    return {(c.ticker, c.timeframe, c.play_type): c for c in rows}


def _analyst_block(
    session: Session, s: Signal,
    calls: dict[tuple[str, str, str], AnalystCall],
    calib_cache: dict[str, dict],
) -> dict[str, object] | None:
    """The ConvictionChip's data: today's grade for this pick plus that grade's
    scored record, or None when no call exists (the chip is simply absent). The
    (n, mean_r) comes from ``analyst_calibration`` over the play type's SCORED
    calls -- the same pure helper the reflection and the retired dashboard used,
    so every surface agrees by construction. A grade with no scored history is an
    honest ``n=0`` / ``mean_r: null`` ("unproven"), never a guessed number.
    Calibration is computed at most once per play type per request (the cache
    dict the endpoint threads through)."""
    call = calls.get((s.ticker, s.timeframe, s.play_type))
    if call is None:
        return None
    if s.play_type not in calib_cache:
        calib_cache[s.play_type] = analyst_calibration(
            load_scored_analyst_calls(session, play_type=s.play_type))
    scored = calib_cache[s.play_type]["by_conviction"].get(call.final_conviction)
    n, mean_r = scored if scored is not None else (0, None)
    return {"grade": call.final_conviction, "n": n, "mean_r": mean_r}


def _pick_row(s: Signal, price: float | None, *, run_d: date,
              analyst: dict[str, object] | None) -> dict[str, object]:
    """One pick's wire row (hand-rolled, like every cockpit serializer). Levels are
    the engine's VERBATIM -- nothing here recomputes a price. ``actionability`` is
    total: ``classify`` answers ``unknown`` for a missing quote or a degenerate
    zone, so the field is always present and never green-by-default. ``is_repeat``
    is the retired dashboard's rule: the setup's streak started on an EARLIER run
    (legacy null first_seen reads as not-a-repeat, fail-open)."""
    act = classify(entry_floor=s.entry_floor, entry_ceiling=s.entry_ceiling,
                   stop=s.stop, price=price)
    return {
        "signal_id": s.id,
        "ticker": s.ticker,
        "play_type": s.play_type,
        "timeframe": s.timeframe,
        "horizon": s.horizon,
        "rank": s.rank,
        "score": s.score,
        "strength": s.strength,
        "conviction_tier": s.conviction_tier,
        "entry_floor": s.entry_floor,
        "entry_ceiling": s.entry_ceiling,
        "stop": s.stop,
        "target": s.target,
        "last_close": price,
        "actionability": {"status": act.status, "dist_r": act.dist_r},
        "is_repeat": s.first_seen_date is not None and s.first_seen_date < run_d,
        "has_chart": s.chart_path is not None,
        "cohort": {"play_type": s.play_type, "strength": s.strength},
        "analyst": analyst,
    }
