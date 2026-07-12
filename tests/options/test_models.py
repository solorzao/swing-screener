from datetime import date, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import BrokerFill, GexSnapshot, OptionPaperTrade, OptionSetup
from swing_screener.db.session import get_engine


def test_lab_tables_roundtrip() -> None:
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        snap = GexSnapshot(
            underlying="SPY", ts=datetime(2026, 7, 13, 9, 10), spot=560.25,
            call_wall=565.0, put_wall=550.0, gamma_flip=557.5,
            net_gex=1.2e9, regime="positive", profile_json="[]", source="computed",
        )
        s.add(snap)
        s.commit()
        setup = OptionSetup(
            ts=datetime(2026, 7, 13, 10, 5), underlying="SPY", direction="long",
            gex_snapshot_id=snap.id, regime="positive", pivot_level=557.5,
            pattern="bull flag at flip", grade="A+", status="taken",
            entry=558.0, stop=556.5, target=565.0,
            chk_daily_bias_clear=True, chk_daily_stack_ordered=True, chk_m5_agrees=True,
            chk_gex_levels_marked=True, chk_price_at_pivot=True, chk_regime_match=True,
            chk_pattern_clean=True, chk_volume_confirming=True, chk_risk_sized=True,
            chk_stop_structural=True, chk_rr_at_least_2=True, chk_confirmation_candle=True,
        )
        s.add(setup)
        s.commit()
        trade = OptionPaperTrade(
            setup_id=setup.id, account="options-lab", strategy="gex",
            underlying="SPY", direction="long",
            opened_at=datetime(2026, 7, 13, 10, 6), entry=558.0, stop=556.5, target=565.0,
        )
        fill = BrokerFill(
            import_hash="abc123", activity_date=date(2026, 7, 10), underlying="PATH",
            occ_symbol="PATH  260717C00013000", trans_code="BTO",
            quantity=5, price=0.05, amount=-25.20, raw='{"x": 1}', source="robinhood",
        )
        s.add_all([trade, fill])
        s.commit()
        assert trade.status == "open"
        assert trade.exit_reason is None
        assert fill.id is not None


def test_broker_fill_import_hash_unique() -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        kw = dict(
            import_hash="dup", activity_date=date(2026, 7, 10), underlying="PATH",
            occ_symbol="PATH  260717C00013000", trans_code="BTO",
            quantity=5, price=0.05, amount=-25.20, raw="{}", source="robinhood",
        )
        s.add(BrokerFill(**kw))
        s.commit()
        s.add(BrokerFill(**kw))
        with pytest.raises(IntegrityError):
            s.commit()
