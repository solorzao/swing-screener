from datetime import date

import pytest

from swing_screener.dashboard.pl import position_pl, total_unrealized_pl
from swing_screener.db.models import Trade


def test_winner_above_entry():
    p = position_pl(entry=100.0, stop=90.0, target=120.0, size=10.0, current_price=110.0)
    assert p.unrealized_pl == 100.0          # (110-100)*10
    assert p.unrealized_pct == 0.10          # (110-100)/100
    assert p.r_multiple == 1.0               # (110-100)/(100-90)
    assert p.dist_to_target_pct == pytest.approx((120 - 110) / 110)
    assert p.dist_to_stop_pct == pytest.approx((110 - 90) / 110)


def test_loss_at_stop_is_minus_1r():
    p = position_pl(entry=100.0, stop=90.0, target=120.0, size=5.0, current_price=90.0)
    assert p.r_multiple == -1.0
    assert p.unrealized_pl == -50.0          # (90-100)*5


def test_nonpositive_risk_raises():
    with pytest.raises(ValueError):
        position_pl(entry=100.0, stop=100.0, target=120.0, size=1.0, current_price=101.0)
    with pytest.raises(ValueError):
        position_pl(entry=100.0, stop=105.0, target=120.0, size=1.0, current_price=101.0)


def _trade(ticker: str, *, entry: float, stop: float, target: float, size: float) -> Trade:
    return Trade(
        ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2026, 1, 1),
        entry_price=entry, size=size, stop=stop, target=target,
    )


def test_total_unrealized_pl_skips_missing_quote():
    valid = _trade("AMD", entry=100.0, stop=90.0, target=120.0, size=10.0)
    no_quote = _trade("NVDA", entry=200.0, stop=190.0, target=220.0, size=5.0)
    prices: dict[str, float | None] = {"AMD": 110.0, "NVDA": None}
    # Only AMD is counted: (110-100)*10 = 100.0; NVDA skipped (missing quote).
    assert total_unrealized_pl([valid, no_quote], prices) == 100.0


def test_total_unrealized_pl_skips_malformed_trade():
    valid = _trade("AMD", entry=100.0, stop=90.0, target=120.0, size=10.0)
    malformed = _trade("BAD", entry=100.0, stop=100.0, target=120.0, size=10.0)  # risk == 0
    prices: dict[str, float | None] = {"AMD": 110.0, "BAD": 105.0}
    # BAD raises ValueError (non-positive risk) and is skipped; only AMD counts.
    assert total_unrealized_pl([valid, malformed], prices) == 100.0
