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


def test_exit_alerts_only_real_trades():
    s, _ = _seed()
    alerts = sel.pending_exit_alerts(s, RUN)
    assert len(alerts) == 1 and alerts[0].reason == "stop" and alerts[0].is_paper is False


def _rev(ticker, rank, strength="early", first_seen=None, conviction_tier="base"):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
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
