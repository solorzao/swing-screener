from collections import Counter
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import PaperTrade, Signal
from swing_screener.db.session import get_engine
from swing_screener.pipeline import run


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
        today=date(2024, 4, 1),
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
    )
    assert res.n_paper_opened >= 1  # prior-bar signal filled by the appended next bar


def test_run_is_idempotent_for_same_day(tmp_path, bars, monkeypatch):
    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    today = date(2024, 4, 1)
    kwargs = dict(
        universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=today,
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
