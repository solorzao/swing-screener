from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker
from tests.pipeline._replay_fixtures import synthetic_daily


def test_future_bars_do_not_change_past_fills():
    cfg = StrategyConfig()
    full = synthetic_daily(n=400)
    # Truncate just past a continuation fill (the fixture's pullback at bar ~339
    # opens a trade around 2025-04-21): the cutoff then sits adjacent to a real
    # fill, so any forward-leak -- a detection or fill that peeks at bars beyond
    # the truncation edge -- would change that fill's economics and trip the
    # assertions below. Truncating in the fixture's trigger-free stretch (e.g.
    # n=380) would leave the boundary far from any fill and rob the test of teeth.
    truncated = full.iloc[:342]

    book_full = replay_ticker("SYN", full, cfg, warmup_bars=250, seed=0)
    book_trunc = replay_ticker("SYN", truncated, cfg, warmup_bars=250, seed=0)

    def opened_by(book, cutoff):
        return {(t.ticker, t.timeframe, t.opened_date, t.arm): t
                for t in book if t.opened_date is not None and t.opened_date <= cutoff}

    cutoff = truncated.index[-2].date()
    a = opened_by(book_full, cutoff)
    b = opened_by(book_trunc, cutoff)
    shared = a.keys() & b.keys()
    assert shared, "expected overlapping fills to compare"
    for k in shared:
        # Entry economics are fixed at the fill and must NEVER depend on later bars:
        # a forward-leak in detection or fill resolution would move entry_price /
        # target / initial risk. (The live `stop` is deliberately excluded here --
        # the partial/trail arms ratchet it AFTER the fill, so a still-open trade in
        # the truncated run legitimately carries a different stop than the same trade
        # advanced further in the full run. That is less data, not lookahead.)
        assert a[k].entry_price == b[k].entry_price
        assert a[k].target == b[k].target
        assert a[k].risk == b[k].risk
        # The realized outcome (and the stop at exit) may only be compared once the
        # trade has CLOSED by the cutoff in BOTH runs -- otherwise the truncated run
        # simply hasn't reached the exit bar yet.
        a_closed = a[k].status == "closed" and a[k].exit_date is not None
        b_closed = b[k].status == "closed" and b[k].exit_date is not None
        if a_closed and b_closed and a[k].exit_date <= cutoff and b[k].exit_date <= cutoff:
            assert a[k].stop == b[k].stop
            assert a[k].realized_r == b[k].realized_r
            assert a[k].exit_reason == b[k].exit_reason
