import swing_screener.pipeline.replay as replay_mod
from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker
from swing_screener.signals.frame import build_frame
from tests.pipeline._replay_fixtures import synthetic_daily


def test_future_bars_do_not_change_past_fills():
    # Coarse guard: truncate the series and compare past fills against the full run.
    # This catches MULTI-bar (>=2) forward leaks but is structurally blind to the
    # classic off-by-one -- see test_detection_never_sees_the_fill_bar for that.
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


def test_detection_never_sees_the_fill_bar(monkeypatch):
    # Structural off-by-one guard with teeth. The coarse truncate-and-compare test
    # above only detects forward leaks of >=2 bars; the single most likely lookahead
    # bug is off-by-one -- detecting on enriched.iloc[:i+1] (peeking at the fill bar)
    # or filling against enriched.iloc[i+1]. We spy on the engine entry points AS
    # IMPORTED INTO replay.py, record per-step facts, then assert the invariant
    # against the known enriched frame: at cursor i, detection sees exactly i bars
    # (ends at i-1, strictly before the fill bar) and the fill uses bar i's high/low.
    cfg = StrategyConfig()
    warmup = 250
    daily = synthetic_daily(n=400)
    # Reproduce the driver's one-time enrichment so we own the ground-truth bars.
    enriched = build_frame(daily, cfg)

    detect_lens: list[int] = []
    fill_highs: list[float] = []
    fill_lows: list[float] = []
    fill_dates: list[object] = []

    real_analyze_frames = replay_mod.analyze_frames
    real_open_from_signals = replay_mod.open_from_signals

    def spy_analyze_frames(ticker, frames, cfg_):
        # Record the detection frame's bar count, then call through unchanged.
        detect_lens.append(len(frames["1d"]))
        return real_analyze_frames(ticker, frames, cfg_)

    def spy_open_from_signals(session, candidates, next_bars, *, fill_date, arms):
        high, low = next_bars[("SYN", "1d")]
        fill_highs.append(high)
        fill_lows.append(low)
        fill_dates.append(fill_date)
        return real_open_from_signals(
            session, candidates, next_bars, fill_date=fill_date, arms=arms
        )

    monkeypatch.setattr(replay_mod, "analyze_frames", spy_analyze_frames)
    monkeypatch.setattr(replay_mod, "open_from_signals", spy_open_from_signals)

    replay_ticker("SYN", daily, cfg, warmup_bars=warmup, seed=0)

    # Exactly one analyze_frames + one open_from_signals call per cursor step.
    steps = len(enriched) - warmup
    assert steps > 0
    assert len(detect_lens) == steps
    assert len(fill_highs) == len(fill_lows) == len(fill_dates) == steps

    for j in range(steps):
        i = warmup + j  # the cursor for this step
        # A +1 DETECTION leak (frames["1d"] == enriched.iloc[:i+1]) makes this i+1.
        assert detect_lens[j] == i, (
            f"step {j} (cursor {i}): detection saw {detect_lens[j]} bars, "
            f"expected {i} (frame must end strictly before fill bar i)"
        )
        # A +1 FILL leak (filling against enriched.iloc[i+1]) makes these the
        # next bar's high/low/date.
        assert fill_highs[j] == float(enriched.iloc[i]["high"]), (
            f"step {j} (cursor {i}): fill high used the wrong bar"
        )
        assert fill_lows[j] == float(enriched.iloc[i]["low"]), (
            f"step {j} (cursor {i}): fill low used the wrong bar"
        )
        assert fill_dates[j] == enriched.index[i].date(), (
            f"step {j} (cursor {i}): fill_date used the wrong bar"
        )
