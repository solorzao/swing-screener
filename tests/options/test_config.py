import dataclasses

import pytest

from swing_screener.options.config import GexConfig


def test_defaults_and_frozen() -> None:
    cfg = GexConfig()
    assert cfg.watchlist == ("SPY", "QQQ")
    assert cfg.ema_spans == (9, 21, 50)
    assert cfg.max_expiries >= 1
    assert 0 < cfg.risk_free_rate < 0.10
    assert cfg.min_stack_spacing_pct > 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.risk_free_rate = 0.99  # type: ignore[misc]
