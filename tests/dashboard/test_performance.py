from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.dashboard.ui import ACCENT
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed_paper_trades(url):
    engine = get_engine(url)
    with Session(engine) as s:
        # Varied timeframe + rank, all closed-filled with realized_r and exit_date
        # so breakdown/rank_bucket/equity_curve are all non-empty.
        s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                         rank=1, fill_status="filled", stop=95.0, target=110.0, risk=5.0,
                         status="closed", realized_r=2.0, hold_bars=4,
                         exit_date=date(2026, 1, 5)))
        s.add(PaperTrade(ticker="NVDA", timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=7, fill_status="filled", stop=190.0, target=220.0, risk=10.0,
                         status="closed", realized_r=-1.0, hold_bars=3,
                         exit_date=date(2026, 1, 6)))
        s.add(PaperTrade(ticker="MSFT", timeframe="1w", horizon="long", signal_score=0.7,
                         rank=12, fill_status="filled", stop=300.0, target=340.0, risk=15.0,
                         status="closed", realized_r=1.5, hold_bars=8,
                         exit_date=date(2026, 1, 7)))
        s.commit()


def test_performance_renders_kpis_and_altair_charts(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_paper_trades(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception

    # The 5 KPI metrics still render with their labels.
    labels = {m.label for m in at.metric}
    assert {"Fill rate", "Win rate", "Expectancy R", "Profit factor", "Closed"}.issubset(labels)

    # The charts are now Altair via the ui helpers, not bare st.bar_chart/st.line_chart.
    # st.bar_chart/st.line_chart also emit vega_lite_chart elements, so distinguish
    # the ui helpers by their accent color, which the bare Streamlit charts never set.
    charts = at.get("vega_lite_chart")
    assert len(charts) >= 1
    assert any(ACCENT in str(c.spec) for c in charts)


def test_performance_empty_state_has_no_chart(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf_empty.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception
    assert not at.get("vega_lite_chart")
