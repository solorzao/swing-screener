import logging
from collections import Counter
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.pipeline import run

# The synthetic _firing fixture's trigger sits far past the fast EMA, so the default
# freshness gate would suppress it. These pipeline-mechanics tests (persistence,
# ranking, charts, shadow fills, idempotency) disable the gate so they exercise the
# plumbing, not the anti-chase policy (covered in tests/pipeline/test_analyze.py).
_NO_EXT_GATE = StrategyConfig(max_extension_atr=0.0)


def _r(tf):
    """Lightweight SignalResult stand-in: _digest_chart_indices reads only .timeframe."""
    return SimpleNamespace(timeframe=tf)


def test_digest_chart_indices_unions_global_and_per_timeframe_picks():
    # results are score-sorted (index == global rank-1). The global top-5 is
    # indices 0..4; the per-timeframe cadences (weekly=1wk, monthly=1mo) each pick
    # their own top-5, which can sit BELOW the global top-5 and would otherwise get
    # no chart. Here the 1wk picks at 6,7 and the 1mo pick at 5 must still chart.
    results = [_r("1d"), _r("1d"), _r("4h"), _r("1d"), _r("1d"),
               _r("1mo"), _r("1wk"), _r("1wk")]
    idx = run._digest_chart_indices(results, top_n=5)

    assert set(range(5)) <= set(idx)  # global top-5 always charted (the daily digest)
    assert 5 in idx                   # 1mo pick below the global top-5 (monthly digest)
    assert 6 in idx and 7 in idx      # 1wk picks below the global top-5 (weekly digest)
    assert idx == sorted(idx)         # returned in ascending index order


def test_digest_chart_indices_caps_each_timeframe_at_top_n():
    # seven 1wk signals: the digest only selects the top-5 per timeframe, so the
    # 6th/7th weekly signal is never picked and must not be charted (no waste).
    results = [_r("1wk")] * 7
    assert run._digest_chart_indices(results, top_n=5) == [0, 1, 2, 3, 4]


def _firing(bars):
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def _firing_then_fill_bar(bars):
    df = _firing(bars)
    # append one more bar that trades inside the entry zone (fills the prior signal)
    import pandas as pd
    nxt = pd.DataFrame(
        {"open": [72.0], "high": [75.0], "low": [70.0], "close": [73.0], "volume": [1_000_000.0]},
        index=pd.date_range(df.index[-1], periods=2, freq="D")[1:],
    )
    return pd.concat([df, nxt])


def _write_universe(tmp_path, tickers):
    p = tmp_path / "universe.csv"
    p.write_text("ticker,name,exchange\n" + "\n".join(f"{t},{t} Inc,NYSE" for t in tickers) + "\n")
    return p


def test_run_persists_ranked_signals_and_writes_charts(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    res = run.run_screen(
        universe_path=_write_universe(tmp_path, ["AAPL", "ZZZ"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
        today=date(2024, 4, 1), cfg=_NO_EXT_GATE,
    )
    assert res.n_signals >= 1
    with Session(get_engine(db)) as s:
        sigs = repo.latest_signals(s, date(2024, 4, 1))
        assert any(x.ticker == "AAPL" for x in sigs)
        assert sigs[0].rank == 1
    assert list((tmp_path / "charts").glob("AAPL_1d_*.png"))  # chart written


def _reversal(bars):
    """Flat base, an RSI-crushing decline below the slow EMA, then a strong green flip."""
    rows, p = [], 100.0
    for i in range(46):
        o = p
        c = p + (0.5 if i % 2 else -0.5)
        rows.append({"open": o, "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": c})
        p = c
    for _ in range(14):
        o = p
        c = p - 2.2
        rows.append({"open": o, "high": o + 0.2, "low": c - 0.3, "close": c})
        p = c
    rows.append({"open": p + 0.2, "high": p + 12.5, "low": p - 0.1, "close": p + 12.0,
                 "volume": 3_000_000.0})
    return bars(rows)


def test_run_persists_and_enriches_universe(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)
    monkeypatch.setattr(run, "fetch_market_cap",
                        lambda t, **kw: 2_000_000.0 if t == "AAPL" else None)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(universe_path=_write_universe(tmp_path, ["AAPL", "ZZZ"]), db_url=db,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                   today=date(2024, 4, 1))
    from swing_screener.db import repo
    from swing_screener.db.session import get_engine
    from sqlalchemy.orm import Session
    with Session(get_engine(db)) as s:
        rows = {u.ticker: u for u in repo.list_universe(s)}
    assert set(rows) == {"AAPL", "ZZZ"}            # full seed persisted
    assert rows["AAPL"].market_cap == 2_000_000.0   # enriched (fetched ticker)
    assert rows["AAPL"].avg_dollar_volume is not None
    assert rows["ZZZ"].market_cap is None           # ZZZ never fetched -> no metrics


def test_run_screens_and_charts_a_reversal_play(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _reversal(bars)} if ticker == "GME" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    res = run.run_screen(
        universe_path=_write_universe(tmp_path, ["GME"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=date(2024, 4, 1),
    )
    assert res.n_reversals >= 1
    with Session(get_engine(db)) as s:
        revs = list(s.scalars(select(Signal).where(Signal.play_type == "reversal")))
    assert any(x.ticker == "GME" for x in revs)
    assert revs[0].strength in ("early", "confirmed")
    assert list((tmp_path / "charts").glob("GME_1d_*.png"))  # reversal chart written


def test_run_isolates_failing_tickers(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        if ticker == "BAD":
            raise RuntimeError("boom")
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    res = run.run_screen(
        universe_path=_write_universe(tmp_path, ["BAD", "AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=date(2024, 4, 1),
        cfg=_NO_EXT_GATE,
    )
    assert res.n_signals >= 1  # AAPL still processed despite BAD raising


def test_run_shadow_book_opens_paper_trade(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing_then_fill_bar(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    res = run.run_screen(
        universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=date(2024, 4, 2),
        cfg=_NO_EXT_GATE,
    )
    assert res.n_paper_opened >= 1  # prior-bar signal filled by the appended next bar


def test_run_stamps_extension_and_inherits_first_seen_across_runs(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    kw = dict(universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
              cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", cfg=_NO_EXT_GATE)

    run.run_screen(today=date(2024, 4, 1), **kw)
    run.run_screen(today=date(2024, 4, 2), **kw)  # same setup fires again next run

    with Session(get_engine(db)) as s:
        d1 = next(x for x in repo.latest_signals(s, date(2024, 4, 1)) if x.ticker == "AAPL")
        d2 = next(x for x in repo.latest_signals(s, date(2024, 4, 2)) if x.ticker == "AAPL")
    assert d1.first_seen_date == date(2024, 4, 1)            # fresh on the first run
    assert d2.first_seen_date == date(2024, 4, 1)            # streak start carried forward
    assert d1.extension_atr is not None and d1.extension_atr > 0  # freshness metric persisted


def test_run_books_screen_variant_paper_trades(tmp_path, bars, monkeypatch):
    # The shadow book forward-tests screen variants as a second dimension. Stub the
    # variant set to an alt that DOESN'T gate the firing fixture (it only retunes the
    # target floor) so both books fill, and assert variant-tagged trades are created --
    # default carries all exit arms, the alt only the baseline exit.
    from dataclasses import replace

    from swing_screener.pipeline.arms import BASELINE
    from swing_screener.pipeline.variants import DEFAULT_VARIANT

    def stub_variants(base):
        return {DEFAULT_VARIANT: base, "alt": replace(base, min_target_r=3.0)}
    monkeypatch.setattr(run, "build_screen_variants", stub_variants)

    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing_then_fill_bar(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                   today=date(2024, 4, 2), cfg=_NO_EXT_GATE)

    with Session(get_engine(db)) as s:
        papers = list(s.scalars(select(PaperTrade)))
    variants = {p.variant for p in papers}
    assert {"default", "alt"} <= variants                       # both books exist
    # the alt variant is booked under the baseline exit only (no partial arms)
    alt_arms = {p.arm for p in papers if p.variant == "alt"}
    assert alt_arms == {BASELINE}
    # the default variant still carries the full exit-arm A/B
    assert len({p.arm for p in papers if p.variant == "default"}) >= 2


def _spy_bull(bars):
    """A 220-bar rising SPY daily frame -> last close above its 200-day SMA (bull)."""
    rows, p = [], 100.0
    for _ in range(220):
        rows.append({"open": p, "high": p + 0.1, "low": p - 0.1, "close": p + 0.1})
        p += 0.1
    return bars(rows)


def test_run_tags_fills_with_market_regime(tmp_path, bars, monkeypatch):
    # SPY (the regime proxy) is fetched through the same seam as the universe, so the
    # mock serves a bull SPY frame alongside the firing ticker; every fill is tagged bull.
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        if ticker == "SPY":
            return {"1d": _spy_bull(bars)}
        return {"1d": _firing_then_fill_bar(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
                   cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
                   today=date(2024, 4, 2), cfg=_NO_EXT_GATE)

    with Session(get_engine(db)) as s:
        papers = list(s.scalars(select(PaperTrade)))
    assert papers, "expected the firing fixture to open paper trades"
    assert all(p.market_trend == "bull" for p in papers)
    assert all(p.market_vol in ("calm", "elevated", "high") for p in papers)


def test_run_is_idempotent_for_same_day(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    today = date(2024, 4, 1)
    kwargs = dict(
        universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=today,
        cfg=_NO_EXT_GATE,
    )
    res1 = run.run_screen(**kwargs)
    run.run_screen(**kwargs)  # second run for the SAME day

    with Session(get_engine(db)) as s:
        sigs = list(s.scalars(select(Signal).where(Signal.run_date == today)))

    # exactly one signal per (ticker, timeframe) -- no duplicates from re-running
    keys = Counter((x.ticker, x.timeframe) for x in sigs)
    assert all(count == 1 for count in keys.values())
    assert len(sigs) == res1.n_signals


def test_run_double_fire_does_not_duplicate_or_double_advance(tmp_path, bars, monkeypatch):
    # DST safety: on a spring-forward/fall-back day a UTC cron pair can fire the
    # screen twice for the intended ET hour. The screen has no EmailLog-style guard
    # -- its safety rests on delete+reinsert of signals (delete_signals_for) and of
    # today's paper opens (delete_paper_trades_opened_on) plus advance_open's
    # same-day guard. Pin that a second same-day run leaves BOTH the signals table
    # and the shadow book in exactly the same state.
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing_then_fill_bar(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    today = date(2024, 4, 2)
    kwargs = dict(
        universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=today,
        cfg=_NO_EXT_GATE,
    )

    def _snapshot():
        with Session(get_engine(db)) as s:
            sigs = list(s.scalars(select(Signal).where(Signal.run_date == today)))
            papers = list(s.scalars(select(PaperTrade)))
        sig_keys = Counter((x.ticker, x.timeframe) for x in sigs)
        # row identity (id) changes on delete+reinsert; the VALUES must not.
        paper_state = sorted(
            (p.ticker, p.timeframe, p.opened_date, p.entry_date, p.last_advanced,
             p.status, p.fill_status, p.realized_r)
            for p in papers
        )
        return len(sigs), sig_keys, paper_state

    res1 = run.run_screen(**kwargs)
    n1, keys1, papers1 = _snapshot()
    assert res1.n_paper_opened >= 1  # the appended fill bar opens a paper trade

    res2 = run.run_screen(**kwargs)  # second fire for the SAME ET day
    n2, keys2, papers2 = _snapshot()

    assert all(count == 1 for count in keys1.values())  # no duplicate signals
    assert (n1, keys1) == (n2, keys2) == (res1.n_signals, keys2)
    assert papers1 == papers2  # no duplicate opens, no double-advance
    assert res1.n_paper_opened == res2.n_paper_opened


def test_run_logs_completion_marker(tmp_path, bars, monkeypatch, caplog):
    """SCREEN_RUN_COMPLETE is the contract with the Azure "missing evening screen"
    alert (infra/modules/alerts.bicep greps ContainerAppConsoleLogs_CL for the
    literal token): every run_screen must end by logging it."""
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    with caplog.at_level(logging.INFO, logger="swing_screener.pipeline.run"):
        run.run_screen(
            universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
            cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts",
            today=date(2024, 4, 1), cfg=_NO_EXT_GATE,
        )
    assert any("SCREEN_RUN_COMPLETE" in m for m in caplog.messages)


def test_would_surface_stamps_strength_and_top5_rank():
    """The booking-time surfacing estimate (North Star #7): reversal EARLY is hidden
    under confirmed_only, the premium bar composes, and rank must hold the digest's
    top-5 within the play type."""
    from dataclasses import replace

    from swing_screener.pipeline.analyze import SignalResult
    from swing_screener.pipeline.run import _would_surface

    cfg = StrategyConfig()

    def sig(play_type, strength=None, tier="base"):
        return SignalResult(ticker="X", timeframe="1d", horizon="medium", score=0.5,
                            mtf_aligned=False, quality_tier="", volatility_tier="",
                            oversold=False, trigger_close=100.0, atr=2.0, rsi=50.0,
                            entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                            frame=None, ctx=None, zone=None, play_type=play_type,
                            strength=strength, conviction_tier=tier)

    assert _would_surface(sig("continuation"), 5, cfg) is True
    assert _would_surface(sig("continuation"), 6, cfg) is False        # below the top-5
    assert _would_surface(sig("reversal", "confirmed"), 1, cfg) is True
    assert _would_surface(sig("reversal", "early"), 1, cfg) is False   # confirmed_only hides EARLY
    assert _would_surface(sig("reversal", "confirmed"), 6, cfg) is False
    prem = replace(cfg, reversal_surface_premium_only=True)
    assert _would_surface(sig("reversal", "confirmed", "premium"), 1, prem) is True
    assert _would_surface(sig("reversal", "confirmed", "base"), 1, prem) is False
