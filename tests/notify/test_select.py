from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db.models import ExitEvent, Signal, Universe
from swing_screener.db.session import get_engine
from swing_screener.notify import select as sel

RUN = date(2026, 6, 15)


def _sig(ticker, tf, rank, first_seen=None):
    return Signal(run_date=RUN, ticker=ticker, timeframe=tf, horizon="medium", score=1.0 / rank,
                  rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0, entry_floor=96.0,
                  entry_ceiling=101.0, stop=95.0, target=110.0, first_seen_date=first_seen)


def _seed():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # ranks are globally unique across timeframes (as the orchestrator assigns
        # them); the 1wk signal MSFT outranks some 1d signals.
        s.add_all([
            _sig("AMD", "1d", 1), _sig("AEP", "1d", 2), _sig("MSFT", "1wk", 3),
            _sig("A", "1d", 4), _sig("AAPL", "1d", 5), _sig("ABBV", "1d", 6),
            _sig("NVDA", "1mo", 7),
        ])
        s.add_all([
            ExitEvent(created_date=RUN, is_paper=False, trade_id=10, tier="hard",
                      reason="stop", message="stopped"),
            ExitEvent(created_date=RUN, is_paper=True, trade_id=99, tier="strong",
                      reason="momentum_flip", message="paper"),
        ])
        s.commit()
        return s, engine  # keep session open for the test


def test_daily_top_n_is_overall_rank_across_timeframes():
    s, _ = _seed()
    picks = sel.daily_picks(s, RUN, top_n=5)
    # top 5 by global rank — includes the rank-3 weekly signal (MSFT), capped at 5
    assert [p.ticker for p in picks] == ["AMD", "AEP", "MSFT", "A", "AAPL"]


def test_daily_picks_dedup_one_ticker_per_list():
    """E4: one ticker firing on multiple timeframes (common in a strong trend) must
    fill ONE top-5 slot -- its best-ranked row -- not several (each surfaced slot is
    a billable Opus deep/conviction call). The freed slots backfill from below in
    rank order, so the list still carries 5 DISTINCT names."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            _sig("AMD", "1d", 1), _sig("AMD", "1wk", 2),  # same name, two timeframes
            _sig("AEP", "1d", 3), _sig("MSFT", "1d", 4), _sig("A", "1d", 5),
            _sig("AAPL", "1d", 6), _sig("ABBV", "1d", 7),
        ])
        s.commit()
        picks = sel.daily_picks(s, RUN, top_n=5)
    # 5 distinct tickers: AMD once (best-ranked row first), ABBV backfilled from below
    assert [p.ticker for p in picks] == ["AMD", "AEP", "MSFT", "A", "AAPL"]
    assert picks[0].timeframe == "1d" and picks[0].rank == 1  # the dup's BEST row won


def test_daily_picks_dedup_composes_with_sector_cap():
    """The dedup applies BEFORE the sector cap, so a duplicate row never consumes a
    sector slot (nor a list slot) on the capped branch either."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            _sig("AAPL", "1d", 1), _sig("AAPL", "1wk", 2),  # dup inside a capped sector
            _sig("MSFT", "1d", 3), _sig("NVDA", "1d", 4), _sig("JPM", "1d", 5),
        ])
        s.add_all([
            Universe(ticker="AAPL", sector="Information Technology"),
            Universe(ticker="MSFT", sector="Information Technology"),
            Universe(ticker="NVDA", sector="Information Technology"),
            Universe(ticker="JPM", sector="Financials"),
        ])
        s.commit()
        picks = sel.daily_picks(s, RUN, top_n=3, max_per_sector=2)
    # AAPL once (not twice against the Tech cap), MSFT fills Tech's 2nd slot,
    # NVDA capped out, JPM backfills -- rank order preserved throughout.
    assert [p.ticker for p in picks] == ["AAPL", "MSFT", "JPM"]
    assert picks[0].rank == 1


def test_daily_picks_caps_per_sector():
    """With a sector cap, no more than max_per_sector picks share a sector; lower-ranked
    picks from other sectors are promoted to fill the list."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # ranks 1-3 are all Tech; cap=2 bumps the 3rd Tech (NVDA) for the next non-Tech pick.
        s.add_all([
            _sig("AAPL", "1d", 1), _sig("MSFT", "1d", 2), _sig("NVDA", "1d", 3),
            _sig("JPM", "1d", 4), _sig("XOM", "1d", 5),
        ])
        s.add_all([
            Universe(ticker="AAPL", sector="Information Technology"),
            Universe(ticker="MSFT", sector="Information Technology"),
            Universe(ticker="NVDA", sector="Information Technology"),
            Universe(ticker="JPM", sector="Financials"),
            Universe(ticker="XOM", sector="Energy"),
        ])
        s.commit()
        picks = sel.daily_picks(s, RUN, top_n=3, max_per_sector=2)
    assert [p.ticker for p in picks] == ["AAPL", "MSFT", "JPM"]  # NVDA capped out


def test_daily_picks_without_cap_is_pure_rank():
    """max_per_sector=None (default) -> unchanged rank-ordered behavior, sectors ignored."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_sig("AAPL", "1d", 1), _sig("MSFT", "1d", 2), _sig("NVDA", "1d", 3)])
        s.add_all([Universe(ticker=t, sector="Information Technology")
                   for t in ("AAPL", "MSFT", "NVDA")])
        s.commit()
        assert [p.ticker for p in sel.daily_picks(s, RUN, top_n=3)] == ["AAPL", "MSFT", "NVDA"]


def test_daily_picks_unknown_sector_is_never_capped():
    """Picks with no Universe row / NULL sector are fail-open: the cap never hides them."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_sig("AAA", "1d", 1), _sig("BBB", "1d", 2), _sig("CCC", "1d", 3)])
        s.commit()  # no Universe rows -> sector unknown for all
        picks = sel.daily_picks(s, RUN, top_n=3, max_per_sector=1)
    assert [p.ticker for p in picks] == ["AAA", "BBB", "CCC"]  # all kept despite cap=1


def test_weekly_and_monthly_filter_by_timeframe():
    s, _ = _seed()
    assert [p.ticker for p in sel.weekly_picks(s, RUN)] == ["MSFT"]
    assert [p.ticker for p in sel.monthly_picks(s, RUN)] == ["NVDA"]


def test_continuation_pickers_park_when_surfacing_off():
    """Continuation parking (Q6 NULL, docs/plans/2026-07-25-q6-q7-sweep-results.md) is
    enforced at THIS layer -- the same one the reversal tier/strength flags live at --
    so the digest and the cockpit agree by construction: surface_continuation=False
    empties every continuation picker while the rows stay stored (detection/scoring/
    shadow-booking untouched) and the reversal list is unaffected."""
    s, _ = _seed()
    assert sel.daily_picks(s, RUN, surface_continuation=False) == []
    assert sel.weekly_picks(s, RUN, surface_continuation=False) == []
    assert sel.monthly_picks(s, RUN, surface_continuation=False) == []
    # the function-layer default stays permissive (True), mirroring how the reversal
    # flags default False here while config carries the real posture -- and PROVES the
    # rows are still in the store: parking is surfacing-only.
    assert [p.ticker for p in sel.daily_picks(s, RUN, top_n=2)] == ["AMD", "AEP"]


def test_exit_alerts_only_real_trades():
    s, _ = _seed()
    alerts = sel.pending_exit_alerts(s, RUN)
    assert len(alerts) == 1 and alerts[0].reason == "stop" and alerts[0].is_paper is False


def test_manual_close_never_emails():
    """A cockpit manual close writes a real (is_paper=False) ExitEvent so the change
    token moves, but the hourly exit job must NEVER email an urgent alert about a
    close Oliver just performed himself -- reason='manual_close' is excluded."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier="hard",
                      reason="stop", message="AMD stopped @ 95"),
            ExitEvent(created_date=RUN, is_paper=False, trade_id=2, tier="",
                      reason="manual_close", message="AEP closed manually @ 110"),
        ])
        s.commit()
        alerts = sel.pending_exit_alerts(s, RUN)
        assert [a.reason for a in alerts] == ["stop"]  # the stop still alerts
        # a day with ONLY a manual close is a no-alert day, not an empty-alert email
        s.query(ExitEvent).filter(ExitEvent.reason == "stop").delete()
        s.commit()
        assert sel.pending_exit_alerts(s, RUN) == []


def _rev(ticker, rank, strength="early", first_seen=None, conviction_tier="base",
         timeframe="1d"):
    return Signal(run_date=RUN, ticker=ticker, timeframe=timeframe, horizon="medium",
                  play_type="reversal", strength=strength, conviction_tier=conviction_tier,
                  score=1.0 / rank, rank=rank,
                  trigger_close=50.0, atr=2.0, rsi=22.0, entry_floor=50.0, entry_ceiling=52.0,
                  stop=47.0, target=58.0, first_seen_date=first_seen)


def test_reversal_picks_premium_only_surfaces_premium_tier():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("AAA", 1, "confirmed", conviction_tier="premium"),
                   _rev("BBB", 2, "confirmed", conviction_tier="strong"),
                   _rev("CCC", 3, "early", conviction_tier="base")])
        s.commit()
        prem = sel.reversal_picks(s, RUN, premium_only=True)
        assert [p.ticker for p in prem] == ["AAA"]   # only the premium tier surfaces
        # default (no filter) keeps all, by rank
        assert [p.ticker for p in sel.reversal_picks(s, RUN)] == ["AAA", "BBB", "CCC"]


def test_reversal_surface_parity_between_screen_and_digest():
    """The screen's ``run._passes_reversal_surface`` and the digest's ``reversal_picks``
    are the SAME tier/strength predicate expressed twice (one over ``SignalResult``, one
    as SQL); their divergence caused the 2026-06-28 drought (an elif let premium_only
    silently override confirmed_only in the digest while the screen composed them).
    Across ALL four flag combos and the four tier x strength cells, the set the screen
    would surface must equal the set the digest surfaces.

    The digest-only stages (staleness cooldown, already-ran drop, sector cap, top-N trim)
    are neutralized -- ``max_age_days=None``, ``top_n`` above the row count, no caller-side
    filters -- so ONLY the tier/strength predicate discriminates."""
    from dataclasses import replace
    from itertools import product

    from swing_screener.config import StrategyConfig
    from swing_screener.pipeline.analyze import SignalResult
    from swing_screener.pipeline.run import _passes_reversal_surface

    # One signal per tier x strength cell, mirrored as a stored Signal row (digest side)
    # and a SignalResult (screen side) with identical tier/strength.
    cells = [("PCON", "premium", "confirmed"), ("PEAR", "premium", "early"),
             ("BCON", "base", "confirmed"), ("BEAR", "base", "early")]

    def _sr(ticker, tier, strength):
        return SignalResult(ticker=ticker, timeframe="1d", horizon="medium", score=0.5,
                            mtf_aligned=False, quality_tier="", volatility_tier="",
                            oversold=False, trigger_close=50.0, atr=2.0, rsi=22.0,
                            entry_floor=50.0, entry_ceiling=52.0, stop=47.0, target=58.0,
                            frame=None, ctx=None, zone=None, play_type="reversal",
                            strength=strength, conviction_tier=tier)

    results = [_sr(t, tier, strength) for t, tier, strength in cells]
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev(t, rank, strength, conviction_tier=tier)
                   for rank, (t, tier, strength) in enumerate(cells, start=1)])
        s.commit()
        for premium_only, confirmed_only in product((False, True), repeat=2):
            cfg = replace(StrategyConfig(),
                          reversal_surface_premium_only=premium_only,
                          reversal_surface_confirmed_only=confirmed_only)
            screen = {sr.ticker for sr in results if _passes_reversal_surface(sr, cfg)}
            digest = {p.ticker for p in sel.reversal_picks(
                s, RUN, top_n=len(cells) + 1, max_age_days=None,
                premium_only=cfg.reversal_surface_premium_only,
                confirmed_only=cfg.reversal_surface_confirmed_only)}
            assert screen == digest, (
                f"screen/digest surfacing diverged for premium_only={premium_only}, "
                f"confirmed_only={confirmed_only}: screen={screen}, digest={digest}"
            )
        # Both flags set must COMPOSE (AND) -- the exact regression the elif caused.
        both = {p.ticker for p in sel.reversal_picks(
            s, RUN, top_n=len(cells) + 1, premium_only=True, confirmed_only=True)}
        assert both == {"PCON"}


def test_reversal_filters_compose_instead_of_premium_overriding():
    """premium_only AND confirmed_only both set -> both apply (AND). The old elif let
    premium silently override confirmed_only, which is how the 2026-06-28 premium-only
    regression blanked the reversal list without anyone noticing."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("AAA", 1, "early", conviction_tier="premium"),
                   _rev("BBB", 2, "confirmed", conviction_tier="premium"),
                   _rev("CCC", 3, "confirmed", conviction_tier="base")])
        s.commit()
        both = sel.reversal_picks(s, RUN, premium_only=True, confirmed_only=True)
        assert [p.ticker for p in both] == ["BBB"]  # premium AND confirmed


def test_reversal_picks_dedup_one_ticker_per_pool():
    """E4, reversal side: the ``REVERSAL_POOL_N`` pool feeds the sector-cap backfill,
    so a ticker firing reversals on two timeframes would hold two pool (and possibly
    two top-5) slots. Keep only its best-ranked row; backfill from below."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            _rev("GME", 1), _rev("GME", 2, timeframe="1wk"),  # same name, two TFs
            _rev("AMC", 3), _rev("BBBY", 4),
        ])
        s.commit()
        picks = sel.reversal_picks(s, RUN, top_n=2)
    # GME once (its rank-1 1d row), AMC backfilled into the freed slot
    assert [p.ticker for p in picks] == ["GME", "AMC"]
    assert picks[0].timeframe == "1d" and picks[0].rank == 1


def test_cap_signals_by_sector_caps_and_backfills():
    """The reversal-list sector cap: a hot sector keeps at most max_per_sector slots and
    lower-ranked names from other sectors backfill (2026-07-02: 31 same-day confirmations
    crowded every software rotation name out of the top-5)."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([Universe(ticker="AAA", sector="Financials"),
                   Universe(ticker="BBB", sector="Financials"),
                   Universe(ticker="CCC", sector="Financials"),
                   Universe(ticker="SFT", sector="Information Technology")])
        s.commit()
        sigs = [_rev("AAA", 1), _rev("BBB", 2), _rev("CCC", 3), _rev("SFT", 4)]
        capped = sel.cap_signals_by_sector(s, sigs, max_per_sector=2, limit=3)
        assert [p.ticker for p in capped] == ["AAA", "BBB", "SFT"]  # CCC capped out
        # None -> pure trim, no sector logic
        assert [p.ticker for p in
                sel.cap_signals_by_sector(s, sigs, max_per_sector=None, limit=3)] == [
            "AAA", "BBB", "CCC"]


def test_cooldown_drops_stale_repeats_keeps_fresh_and_legacy():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([
            _sig("AMD", "1d", 1, first_seen=RUN),             # first seen today
            _sig("AEP", "1d", 2, first_seen=date(2026, 6, 1)),  # old streak (stale)
            _sig("MSFT", "1d", 3),                            # first_seen None (legacy)
        ])
        s.commit()

        # cooldown of 1 day -> AEP (first seen 14 days ago) drops; today's + legacy stay.
        picks = sel.daily_picks(s, RUN, max_age_days=1)
        assert [p.ticker for p in picks] == ["AMD", "MSFT"]
        # no cooldown -> all three, by rank
        assert [p.ticker for p in sel.daily_picks(s, RUN)] == ["AMD", "AEP", "MSFT"]


def test_cooldown_applies_to_reversal_picks():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("GME", 1, first_seen=RUN),
                   _rev("BBBY", 2, first_seen=date(2026, 6, 1))])
        s.commit()
        assert [p.ticker for p in sel.reversal_picks(s, RUN, max_age_days=1)] == ["GME"]


def test_cooldown_counts_runs_not_calendar_days_across_a_weekend():
    """A pick first seen on FRIDAY's run must still show in MONDAY's digest under
    cooldown=1: freshness counts SCREEN RUNS (the distinct run_dates the system actually
    experienced), not calendar days -- the old arithmetic computed Monday-1=Sunday and
    dropped every Friday-fresh setup from Monday's email (2026-07 audit)."""
    friday = date(2026, 6, 12)  # RUN (2026-06-15) is the following Monday
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        # Friday's run: the setup appears fresh...
        s.add(Signal(run_date=friday, ticker="AMD", timeframe="1d", horizon="medium",
                     score=0.9, rank=1, trigger_close=100.0, atr=4.0, rsi=55.0,
                     entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                     first_seen_date=friday))
        # ...and Monday's run re-detects it, streak carried (first_seen still Friday).
        s.add(_sig("AMD", "1d", 1, first_seen=friday))
        s.add(_sig("MSFT", "1d", 2, first_seen=date(2026, 6, 1)))  # genuinely stale
        s.commit()

        picks = sel.daily_picks(s, RUN, max_age_days=1)
        assert [p.ticker for p in picks] == ["AMD"]  # Friday-fresh survives Monday


def test_reversal_picks_confirmed_only_drops_early():
    """confirmed_only=True keeps only CONFIRMED-strength reversals (the cost-robust edge);
    EARLY is still in the DB (shadow-tracked) but hidden from the digest. Default keeps all."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("GME", 1, "confirmed"), _rev("BBBY", 2, "early"),
                   _rev("AMC", 3, "confirmed")])
        s.commit()
        # default: all strengths, by rank
        assert [p.ticker for p in sel.reversal_picks(s, RUN)] == ["GME", "BBBY", "AMC"]
        # confirmed_only: EARLY (BBBY) dropped, rank order preserved
        conf = sel.reversal_picks(s, RUN, confirmed_only=True)
        assert [p.ticker for p in conf] == ["GME", "AMC"]
        assert all(p.strength == "confirmed" for p in conf)


def test_reversal_funnel_counts_detected_and_confirmed():
    """The funnel counts EVERYTHING stored for the run date (no cooldown, no tier filter):
    they exist so the digest can say 'N detected, M confirmed' even when the surfaced
    list is empty -- a filtered-out day must be distinguishable from a quiet market."""
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_rev("GME", 1, "confirmed"), _rev("BBBY", 2, "early"),
                   _rev("AMC", 3, "confirmed", first_seen=date(2026, 6, 1))])  # stale too
        s.add_all([_sig("AMD", "1d", 4)])  # continuation is not counted
        s.commit()
        assert sel.reversal_funnel(s, RUN) == (3, 2)


def test_reversal_funnel_empty_day():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        assert sel.reversal_funnel(s, RUN) == (0, 0)


def test_reversal_picks_are_separate_from_continuation():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add_all([_sig("AMD", "1d", 1), _sig("AEP", "1d", 2)])           # continuation
        s.add_all([_rev("GME", 1, "confirmed"), _rev("BBBY", 2, "early")])  # reversal
        s.commit()

        cont = sel.daily_picks(s, RUN)
        rev = sel.reversal_picks(s, RUN)
        assert [p.ticker for p in cont] == ["AMD", "AEP"]   # reversals excluded
        assert [p.ticker for p in rev] == ["GME", "BBBY"]   # only reversals, by rank
        assert rev[0].strength == "confirmed" and rev[0].play_type == "reversal"
