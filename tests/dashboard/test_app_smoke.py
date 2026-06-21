from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.dashboard import quotes
from swing_screener.db.models import AnalystCall, PaperTrade, Signal, Trade
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Signal(run_date=date.today(), ticker="AMD", timeframe="1d", horizon="medium",
                     score=0.9, rank=1, trigger_close=100.0, atr=4.0, rsi=55.0,
                     entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0))
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=95.0, target=110.0))
        s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                         rank=1, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=2.0, hold_bars=4, exit_date=date.today()))
        s.commit()


ALL_PAGES = ["Overview", "Today's Candidates", "Deep Analysis", "Active Trades",
             "Trade Entry", "Closed Trades", "Screener Performance",
             "Analyst Calibration", "Exit Log", "Universe", "Digest Log"]


def test_app_renders_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert at.sidebar.radio[0].value == "Overview"  # default landing page


def test_every_page_renders_with_seeded_data(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    _seed(url)
    at = AppTest.from_file(APP).run()
    for page in ALL_PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page} raised"


def test_seeded_ticker_surfaces_on_candidates(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    # 100.0 sits inside the seeded entry zone [96, 101] -> actionable -> shown by default
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 100.0})
    _seed(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Today's Candidates").run()
    rendered = " ".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "AMD" in rendered or any("AMD" in str(df.value.to_string()) for df in at.dataframe)


def test_already_ran_pick_hidden_by_default_shown_when_unfiltered(tmp_path, monkeypatch):
    # AMD's latest price (104) has run above its entry ceiling (101): "already ran".
    # The default "hide" filter drops it; unticking the checkbox brings it back.
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    _seed(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Today's Candidates").run()

    default_tables = " ".join(df.value.to_string() for df in at.dataframe)
    assert "AMD" not in default_tables  # hidden by default -- it already ran

    at.checkbox[0].set_value(False).run()  # show plays that already ran
    shown_tables = " ".join(df.value.to_string() for df in at.dataframe)
    assert "AMD" in shown_tables
    assert "already ran" in shown_tables  # the status label is rendered


def test_repeat_pick_hidden_by_default_shown_when_unfiltered(tmp_path, monkeypatch):
    # AMD's setup first appeared on an earlier run (first_seen < run_date) -> a repeat.
    # The default repeat filter hides it; unticking it brings the repeat back.
    url = f"sqlite:///{tmp_path / 'dash.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 100.0})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Signal(run_date=date.today(), ticker="AMD", timeframe="1d", horizon="medium",
                     play_type="continuation", score=0.9, rank=1, trigger_close=100.0,
                     atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
                     stop=95.0, target=110.0,
                     first_seen_date=date.today() - timedelta(days=3)))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Today's Candidates").run()

    assert "AMD" not in " ".join(df.value.to_string() for df in at.dataframe)  # repeat hidden
    at.checkbox[1].set_value(False).run()  # checkbox[0]=hide-ran, checkbox[1]=hide-repeats
    assert "AMD" in " ".join(df.value.to_string() for df in at.dataframe)


def test_play_type_filter_keeps_only_reversal(tmp_path, monkeypatch):
    # Seed two signals on the same run_date — one continuation, one reversal — then
    # set the main-area play-type filter to "Reversal" and assert only the reversal
    # ticker surfaces in the dataframe (continuation is filtered out).
    url = f"sqlite:///{tmp_path / 'filter.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Signal(run_date=date.today(), ticker="AMD", timeframe="1d", horizon="medium",
                     play_type="continuation", score=0.9, rank=1, trigger_close=100.0,
                     atr=4.0, rsi=55.0, entry_floor=96.0, entry_ceiling=101.0,
                     stop=95.0, target=110.0))
        s.add(Signal(run_date=date.today(), ticker="NVDA", timeframe="1d", horizon="medium",
                     play_type="reversal", strength="confirmed", oversold=True, score=0.8,
                     rank=2, trigger_close=200.0, atr=8.0, rsi=28.0, entry_floor=190.0,
                     entry_ceiling=202.0, stop=188.0, target=220.0))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Today's Candidates").run()

    # Sanity: under "All" both tickers are in the table and the new columns exist.
    all_table = " ".join(df.value.to_string() for df in at.dataframe)
    assert "AMD" in all_table and "NVDA" in all_table
    cols = list(at.dataframe[0].value.columns)
    assert {"rank", "play_type", "rsi"}.issubset(set(cols))

    # The play-type control lives in the MAIN area (sidebar nav is at.sidebar.radio).
    at.segmented_control[0].set_value("Reversal").run()
    reversal_table = " ".join(df.value.to_string() for df in at.dataframe)
    assert "NVDA" in reversal_table
    assert "AMD" not in reversal_table


def test_analyst_calibration_surfaces_scored_calls(tmp_path, monkeypatch):
    # A scored AnalystCall (high conviction, +2.0R) must surface on the calibration page.
    url = f"sqlite:///{tmp_path / 'calib.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(AnalystCall(
            created_date=date.today(), ticker="AMD", timeframe="1d",
            play_type="continuation", run_date=date.today(), baseline_conviction="medium",
            final_conviction="high", nudge_reason="x", model="claude-opus-4-8",
            realized_r=2.0, scored_at=date.today()))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Analyst Calibration").run()
    assert not at.exception
    rendered = " ".join(str(getattr(el, "value", "")) for el in at.markdown)
    tables = " ".join(df.value.to_string() for df in at.dataframe)
    assert "Continuation" in rendered
    assert "high" in tables


def test_overview_reflects_open_position_count(tmp_path, monkeypatch):
    # Seed one open Trade + a live quote; the Overview "Open positions" KPI
    # must report 1.
    url = f"sqlite:///{tmp_path / 'overview.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 104.0})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=95.0, target=110.0))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Overview").run()
    assert not at.exception
    assert any(m.label == "Open positions" and m.value == "1" for m in at.metric)


def test_active_trades_formats_and_colors_positive_pl(tmp_path, monkeypatch):
    # Seed an open trade with a live quote ABOVE entry -> positive unrealized P/L.
    # This drives the Styler format/color path (ui.fmt_money/fmt_pct/pl_color), so
    # rendering must not raise and the underlying DataFrame must carry the numeric,
    # positive unrealized_$ (the format/color is applied on top via the Styler).
    url = f"sqlite:///{tmp_path / 'pl.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"AMD": 110.0})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Trade(ticker="AMD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=95.0, target=120.0))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Active Trades").run()
    assert not at.exception

    # The first dataframe on the page is the P/L table. AppTest exposes the
    # underlying (pre-Styler) DataFrame as .value; assert the numeric P/L is present
    # and positive ((110 - 100) * 10 = 100.0).
    df = at.dataframe[0].value
    assert "unrealized_$" in df.columns
    pl_values = [v for v in df["unrealized_$"].tolist() if v is not None]
    assert pl_values and pl_values[0] > 0


def test_active_trades_survives_malformed_trade(tmp_path, monkeypatch):
    # a zero-risk trade (stop == entry) makes position_pl raise; with a live quote
    # available the Active Trades view must not crash (the render guard catches it).
    url = f"sqlite:///{tmp_path / 'bad.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    monkeypatch.setattr(quotes, "latest_closes", lambda tickers, **kw: {"BAD": 50.0})
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(Trade(ticker="BAD", timeframe="1d", horizon="medium", entry_date=date.today(),
                    entry_price=100.0, size=10.0, stop=100.0, target=110.0))
        s.commit()
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Active Trades").run()
    assert not at.exception
