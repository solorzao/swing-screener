import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from swing_screener.db.models import GexSnapshot, OptionPaperTrade
from swing_screener.db.session import get_engine
from swing_screener.options import run as run_mod
from swing_screener.options.chain import ChainSnapshot
from swing_screener.options.config import GexConfig
from swing_screener.options.run import run_analyze, run_import, run_plan

_FIXTURE = Path(__file__).parent / "fixtures" / "robinhood_sample.csv"


def _fake_snapshotter(ticker: str, cfg: GexConfig) -> ChainSnapshot:
    frame = pd.DataFrame([
        {"expiry": date(2026, 7, 17), "strike": 105.0, "right": "C",
         "open_interest": 50_000, "iv": 0.2},
        {"expiry": date(2026, 7, 17), "strike": 95.0, "right": "P",
         "open_interest": 40_000, "iv": 0.25},
    ])
    return ChainSnapshot(underlying=ticker, spot=100.0,
                         asof=datetime(2026, 7, 13, 9, 10), frame=frame)


def _fake_daily(ticker: str) -> pd.DataFrame:
    return pd.DataFrame({
        "open": range(100, 220), "high": range(101, 221), "low": range(99, 219),
        "close": range(100, 220), "volume": [1_000_000] * 120,
    }, index=pd.date_range("2026-01-01", periods=120))


def test_run_plan_persists_snapshots_and_returns_plans() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        plans = run_plan(s, cfg=GexConfig(watchlist=("SPY",)),
                         snapshotter=_fake_snapshotter, daily_bars=_fake_daily)
        assert len(plans) == 1
        assert plans[0].underlying == "SPY"
        rows = s.query(GexSnapshot).all()
        assert len(rows) == 1 and rows[0].underlying == "SPY"


def test_run_analyze_no_save_writes_nothing() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        levels, liq = run_analyze("NVDA", cfg=GexConfig(),
                                  snapshotter=_fake_snapshotter, session=s)
        assert levels.call_wall == 105.0
        assert liq.thin is True  # 2-strike fixture trips the liquidity guard
        assert s.query(GexSnapshot).count() == 0


def test_run_import_tag_all_commits_closed_episodes() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        out = run_import(s, _FIXTURE, tag_all="other")
        assert "committed" in out.lower()
        rows = s.query(OptionPaperTrade).all()
        assert rows and all(r.account == "robinhood" and r.strategy == "other" for r in rows)


def test_run_import_without_tag_all_commits_nothing() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        out = run_import(s, _FIXTURE, tag_all=None)
        assert s.query(OptionPaperTrade).count() == 0
        assert "review" in out.lower()


def _settle_argv(monkeypatch, *extra: str) -> None:
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("SWING_REQUIRE_DB", raising=False)
    monkeypatch.setattr(sys, "argv",
                        ["gex", "--db", "sqlite:///:memory:", "settle", *extra])


def test_settle_cli_refuses_before_close_without_force(monkeypatch) -> None:
    """An intraday `settle` run would eod_flat-flatten trades at a partial-session
    price AND pin the day-keyed 5m cache on the partial session -- refuse it
    outright before 16:00 ET (2026-07-17 audit, H2)."""
    monkeypatch.setattr(run_mod, "_now_eastern", lambda: datetime(2026, 7, 13, 13, 0))
    _settle_argv(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        run_mod.main()
    msg = str(excinfo.value)
    assert "16:00" in msg and "--force" in msg


def test_settle_cli_force_overrides_pre_close_refusal(monkeypatch) -> None:
    monkeypatch.setattr(run_mod, "_now_eastern", lambda: datetime(2026, 7, 13, 13, 0))
    _settle_argv(monkeypatch, "--force")
    run_mod.main()  # proceeds (empty book settles nothing); no SystemExit


def test_settle_cli_proceeds_after_close(monkeypatch) -> None:
    monkeypatch.setattr(run_mod, "_now_eastern", lambda: datetime(2026, 7, 13, 16, 5))
    _settle_argv(monkeypatch)
    run_mod.main()  # post-close runs never need --force


def test_cache_dir_flag_reaches_the_fetch_path(tmp_path, monkeypatch) -> None:
    """--cache-dir was parsed but silently ignored -- the default fetchers resolved
    load_settings().cache_dir instead (2026-07-17 audit, H3). End-to-end through
    main(): the flag's value must be the cache_dir the fetch seam receives."""
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    with Session(get_engine(url)) as s:
        s.add(OptionPaperTrade(
            account="options-lab", strategy="gex", underlying="SPY", direction="long",
            opened_at=datetime(2026, 7, 13, 9, 35), entry=100.0, stop=99.0, target=102.0))
        s.commit()

    seen: dict[str, object] = {}

    def fake_fetch(ticker, interval, *, cache_dir, **kw):
        seen["cache_dir"] = cache_dir
        idx = pd.date_range("2026-07-13 15:45", periods=3, freq="5min")
        return pd.DataFrame({"open": 100.0, "high": 100.6, "low": 99.6,
                             "close": 100.5, "volume": 1e6}, index=idx)

    monkeypatch.setattr(run_mod, "fetch_bars", fake_fetch)
    monkeypatch.setattr(run_mod, "_now_eastern", lambda: datetime(2026, 7, 13, 16, 5))
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("SWING_REQUIRE_DB", raising=False)
    custom = tmp_path / "custom-cache"
    monkeypatch.setattr(sys, "argv",
                        ["gex", "--db", url, "--cache-dir", str(custom), "settle"])
    run_mod.main()
    assert seen["cache_dir"] == custom
