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


def _seed_two_arms(url):
    engine = get_engine(url)
    with Session(engine) as s:
        # same fill under two arms, different realized R (the dual-book A/B)
        for arm, r in (("baseline", 1.0), ("partial33_cond", 1.6)):
            s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                             rank=1, arm=arm, fill_status="filled", stop=95.0, target=110.0,
                             risk=5.0, status="closed", realized_r=r, hold_bars=4,
                             exit_date=date(2026, 1, 5)))
        s.commit()


def test_performance_shows_per_arm_ab_when_multiple_arms(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf_arms.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_two_arms(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception

    # The arm-detail selector appears in the main area (the only non-sidebar radio),
    # defaulting to the baseline arm.
    main_radios = [r for r in at.radio if r.value in ("baseline", "partial33_cond")]
    assert main_radios and main_radios[0].value == "baseline"

    # Baseline arm: expectancy R = 1.0. Switch to the partial arm -> 1.6.
    def _expectancy():
        return next(m.value for m in at.metric if m.label == "Expectancy R")

    assert _expectancy() == "1.00"
    main_radios[0].set_value("partial33_cond").run()
    assert _expectancy() == "1.60"


def _seed_mixed_play_types(url):
    engine = get_engine(url)
    with Session(engine) as s:
        rows = [
            ("continuation", "baseline", 1.0),
            ("continuation", "partial33_cond", 1.5),
            ("reversal", "baseline", -0.5),
            ("reversal", "partial33_cond", 0.5),
        ]
        for play, arm, r in rows:
            s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                             rank=1, play_type=play, arm=arm, fill_status="filled", stop=95.0,
                             target=110.0, risk=5.0, status="closed", realized_r=r, hold_bars=4,
                             exit_date=date(2026, 1, 5)))
        s.commit()


def test_performance_play_type_filter_scopes_the_arm_ab(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf_mixed.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_mixed_play_types(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception

    def _expectancy():
        return next(m.value for m in at.metric if m.label == "Expectancy R")

    # Default "All" baseline arm: mean(1.0, -0.5) = 0.25.
    assert _expectancy() == "0.25"
    # Scope to reversal -> baseline arm sees only the -0.5 reversal trade.
    at.segmented_control[0].set_value("Reversal").run()
    assert not at.exception
    assert _expectancy() == "-0.50"


def _seed_two_variants(url):
    engine = get_engine(url)
    with Session(engine) as s:
        # two screen variants, each on the baseline exit arm, different realized R
        s.add(PaperTrade(ticker="AMD", timeframe="1d", horizon="medium", signal_score=0.9,
                         rank=1, arm="baseline", variant="default", fill_status="filled",
                         stop=95.0, target=110.0, risk=5.0, status="closed", realized_r=1.0,
                         hold_bars=4, exit_date=date(2026, 1, 5)))
        s.add(PaperTrade(ticker="NVDA", timeframe="1d", horizon="medium", signal_score=0.8,
                         rank=1, arm="baseline", variant="extguard_tight", fill_status="filled",
                         stop=190.0, target=220.0, risk=10.0, status="closed", realized_r=2.0,
                         hold_bars=3, exit_date=date(2026, 1, 6)))
        s.commit()


def test_performance_shows_strategy_leaderboard_for_variants(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf_var.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_two_variants(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception

    md = " ".join(str(getattr(m, "value", "")) for m in at.markdown)
    assert "Strategy leaderboard" in md
    tables = " ".join(df.value.to_string() for df in at.dataframe)
    assert "extguard_tight" in tables and "default" in tables
    # downstream KPIs are scoped to the default variant -> expectancy 1.0, not blended w/ 2.0
    assert next(m.value for m in at.metric if m.label == "Expectancy R") == "1.00"


def test_performance_empty_state_has_no_chart(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'perf_empty.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Screener Performance").run()
    assert not at.exception
    assert not at.get("vega_lite_chart")
