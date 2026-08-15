"""``_log_drained_arms``: the signal that a retired arm is safe to delete.

Draining is only self-limiting if something tells the operator when it is finished.
Without this line the removal date is unknowable without a hand-written query, so the
entry lingers and the stepper carries a config nothing needs.
"""

import logging

from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db import repo
from swing_screener.db.models import PaperTrade
from swing_screener.db.session import get_engine
from swing_screener.pipeline.arms import build_draining_arms
from swing_screener.pipeline.run import _log_drained_arms


def _open_row(arm: str) -> PaperTrade:
    return PaperTrade(
        ticker="AAPL", timeframe="1d", horizon="medium", signal_score=0.8, rank=1,
        play_type="continuation", arm=arm, variant="default", fill_status="filled",
        stop=95.0, target=110.0, risk=5.0, status="open",
    )


def test_drain_complete_fires_only_for_arms_with_no_open_rows(caplog):
    cfg = StrategyConfig()
    draining = sorted(build_draining_arms(cfg))
    assert draining, "this test is meaningless with an empty draining set"
    still_open, *finished = draining

    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        repo.save_paper_trades(s, [_open_row(still_open)])
        with caplog.at_level(logging.INFO, logger="swing_screener.pipeline.run"):
            _log_drained_arms(s, cfg)

    logged = [r.getMessage() for r in caplog.records if "DRAIN_COMPLETE" in r.getMessage()]
    assert not any(f"arm={still_open}" in m for m in logged), (
        f"{still_open} still has an open row -- it is not drained"
    )
    for name in finished:
        assert any(f"arm={name}" in m for m in logged), f"{name} is drained but went unreported"


def test_no_signal_when_nothing_is_draining(caplog, monkeypatch):
    # The end state: once every entry has been deleted the probe goes quiet (and never
    # issues an empty IN (), which SQL Server rejects).
    monkeypatch.setattr("swing_screener.pipeline.run.build_draining_arms", lambda _cfg: {})
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s, caplog.at_level(logging.INFO):
        _log_drained_arms(s, StrategyConfig())
    assert not [r for r in caplog.records if "DRAIN_COMPLETE" in r.getMessage()]
