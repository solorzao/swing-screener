"""Weather router (split from test_api.py): /api/weather."""

from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from swing_screener.db.models import (
    MarketReport,
)
from tests.cockpit.conftest import (
    _client_and_engine,
)


WEATHER_KEYS = {"run_date", "ha_alignment", "flipped", "spy_vs_200dma",
                "vol_bucket", "vix", "vix_rank", "vix_spike", "ten_year",
                "three_month", "yield_inverted", "bond_trend", "vix_term_ratio",
                "vix_backwardation", "credit_chg_4w", "credit_pctile",
                "cyc_def_trend", "cyc_def_chg_4w", "breadth_trend",
                "breadth_chg_4w", "recession_prob", "is_deep", "core", "report",
                "created_at"}


def test_weather_null_then_latest(tmp_path: Path) -> None:
    """``{weather: null}`` on an empty table (weekly job; a fresh DB has none --
    a setup state, not an error). With rows, the newest run_date wins, EVERY
    column rides -- including ``is_deep=False``, the deterministic-fallback
    badge that keeps a canned report from passing as the analyst's -- and the
    history is the flip log, newest first."""
    client, engine = _client_and_engine(tmp_path)
    r = client.get("/api/weather")
    assert r.status_code == 200
    assert r.json() == {"weather": None, "history": []}

    with Session(engine) as s:
        s.add(MarketReport(run_date=date(2026, 6, 28), ha_alignment="mixed",
                           flipped=True, core="old core", report="old report",
                           is_deep=True, vix=15.0))
        s.add(MarketReport(run_date=date(2026, 7, 5),
                           ha_alignment="aligned_bull", flipped=False,
                           core="new core", report="full text", is_deep=False,
                           vix=13.2, vix_rank=0.31, spy_vs_200dma="above",
                           yield_inverted=False, recession_prob=0.18,
                           created_at=datetime(2026, 7, 5, 13, 0)))
        s.commit()
    body = client.get("/api/weather").json()
    w = body["weather"]
    assert set(w) == WEATHER_KEYS
    assert w["run_date"] == "2026-07-05"
    assert w["is_deep"] is False  # the fallback badge, served honestly
    assert w["core"] == "new core" and w["report"] == "full text"
    assert w["vix"] == 13.2 and w["spy_vs_200dma"] == "above"
    assert w["yield_inverted"] is False and w["recession_prob"] == 0.18
    assert w["created_at"] == "2026-07-05T13:00:00+00:00"  # unambiguous UTC
    assert body["history"] == [
        {"run_date": "2026-07-05", "ha_alignment": "aligned_bull",
         "flipped": False, "core": "new core"},
        {"run_date": "2026-06-28", "ha_alignment": "mixed",
         "flipped": True, "core": "old core"},
    ]


