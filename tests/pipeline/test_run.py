from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.pipeline import run


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
