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


def test_fmt_compact_usd_abbreviates_magnitude():
    assert ui.fmt_compact_usd(36_247_314_659) == "$36.2B"
    assert ui.fmt_compact_usd(393_058_401_045) == "$393.1B"
    assert ui.fmt_compact_usd(349_451_363) == "$349.5M"
    assert ui.fmt_compact_usd(2_500) == "$2.5K"
    assert ui.fmt_compact_usd(900) == "$900"
    assert ui.fmt_compact_usd(1_500_000_000_000) == "$1.5T"
    assert ui.fmt_compact_usd(None) == "—"
    assert ui.fmt_compact_usd(float("nan")) == "—"


def test_line_builds_chart():
    import altair as alt
    from datetime import date
    lc = ui.line([(date(2026, 1, 1), 1.0), (date(2026, 1, 2), 2.0)], "Date", "Cum R")
    assert isinstance(lc, alt.Chart)
    assert len(lc.data) == 2


def test_connection_label_hides_credentials():
    assert ui.connection_label("sqlite:///local.db") == "Local SQLite"
    azure = "mssql+pyodbc://user:secret@srv.database.windows.net/swing?driver=ODBC+Driver+18"
    label = ui.connection_label(azure)
    assert label == "Azure SQL · swing"
    assert "secret" not in label and "srv.database.windows.net" not in label


def test_connection_label_falls_back_on_garbage():
    assert ui.connection_label("not a url at all ::: ???") == "Database"
