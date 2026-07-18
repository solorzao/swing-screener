import pytest

from swing_screener.analytics.pl import position_pl


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
