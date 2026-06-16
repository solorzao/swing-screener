from swing_screener.dashboard import ui


def test_pct_formats_with_sign():
    assert ui.fmt_pct(0.1234) == "+12.34%"
    assert ui.fmt_pct(-0.05) == "-5.00%"


def test_money_formats_currency():
    assert ui.fmt_money(1234.5) == "$1,234.50"
    assert ui.fmt_money(-12.0) == "-$12.00"


def test_near_zero_never_renders_negative_zero():
    # a tiny negative must not display as '-$0.00' / '-0.00%'
    assert ui.fmt_money(-0.001) == "$0.00"
    assert ui.fmt_pct(-0.00001) == "+0.00%"


def test_pl_color_picks_semantic_color():
    assert ui.pl_color(3.0) == ui.POS
    assert ui.pl_color(-1.0) == ui.NEG
    assert ui.pl_color(0.0) == ui.NEG  # break-even is not a win
