"""The deterministic GEX reading: right claims for each regime, no fabricated
levels, thin warning first, compact dollar formatting."""

from swing_screener.options.reading import describe_gex, fmt_gex_dollars


def test_positive_regime_reads_dampening_and_asymmetry() -> None:
    lines = describe_gex(
        spot=7125.0, call_wall=7200.0, put_wall=6800.0, gamma_flip=6860.0,
        regime="positive", net_gex=52_000_000.0,
    )
    text = "\n".join(lines)
    assert "Positive gamma" in text
    assert "dampens volatility" in text
    assert "Call wall 7,200 (+1.1%)" in text
    assert "Put support 6,800 (−4.6%)" in text
    # The asymmetry callout: the flip below spot names the fragile side.
    assert "Below 6,860" in text and "flips negative" in text
    assert "toward put support 6,800" in text
    assert "$52M" in text
    # The model-honesty closer is always last.
    assert lines[-1].startswith("Model:")


def test_negative_regime_reads_amplification_and_reclaim() -> None:
    lines = describe_gex(
        spot=6700.0, call_wall=7000.0, put_wall=6600.0, gamma_flip=6860.0,
        regime="negative", net_gex=-8_400_000.0,
    )
    text = "\n".join(lines)
    assert "Negative gamma" in text and "amplifies volatility" in text
    assert "Above 6,860" in text and "flips positive" in text
    assert "-$8M" in text


def test_unknown_regime_stands_down_without_numbers() -> None:
    lines = describe_gex(
        spot=100.0, call_wall=None, put_wall=None, gamma_flip=None,
        regime="unknown", net_gex=0.0,
    )
    text = "\n".join(lines)
    assert "stand down" in text
    # No wall/flip sentence may exist for absent levels (never fabricated).
    assert "Call wall" not in text and "Put support" not in text
    # No net-gamma line either: an unknown regime's net is indeterminate.
    assert "Net dealer gamma" not in text


def test_thin_chain_warning_is_first_and_carries_reasons() -> None:
    lines = describe_gex(
        spot=50.0, call_wall=52.0, put_wall=48.0, gamma_flip=49.0,
        regime="positive", thin_chain=True,
        thin_reasons=["total OI 120 < 10000"],
    )
    assert lines[0].startswith("THIN CHAIN")
    assert "total OI 120 < 10000" in lines[0]


def test_fmt_gex_dollars_magnitudes() -> None:
    assert fmt_gex_dollars(1_240_000_000.0) == "$1.2B"
    assert fmt_gex_dollars(340_000_000.0) == "$340M"
    assert fmt_gex_dollars(-8_500_000.0) == "-$8M"
    assert fmt_gex_dollars(120_500.0) == "$120K"
    assert fmt_gex_dollars(0.0) == "$0"
