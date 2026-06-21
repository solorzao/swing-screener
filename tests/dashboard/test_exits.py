from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.db.models import ExitEvent
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def _seed_exits(url):
    engine = get_engine(url)
    with Session(engine) as s:
        # Mixed reason ("stop", "target", "stop") and mixed is_paper. The first paper
        # exit is the curated intent book (account="paper"); the rest default to the
        # research grid (account="research") -- so the Account facet has both to slice.
        s.add(ExitEvent(created_date=date.today(), is_paper=True, trade_id=1,
                        tier="t1", reason="stop", message="real-stop-A", account="paper"))
        s.add(ExitEvent(created_date=date.today(), is_paper=False, trade_id=2,
                        tier="t1", reason="target", message="real-target-B"))
        s.add(ExitEvent(created_date=date.today(), is_paper=False, trade_id=3,
                        tier="t2", reason="stop", message="real-stop-C"))
        s.commit()


def test_exit_log_renders_all_under_defaults(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'exits.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_exits(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Exit Log").run()
    assert not at.exception

    # Default = all reasons selected, "All" book, "All" account -> all three rows present.
    table = at.dataframe[0].value
    assert len(table) == 3
    reasons = set(table["reason"])
    assert reasons == {"stop", "target"}
    messages = set(table["message"])
    assert {"real-stop-A", "real-target-B", "real-stop-C"} == messages
    # the account column is rendered, carrying both books under the default view.
    assert set(table["account"]) == {"paper", "research"}


def test_exit_log_reason_multiselect_filters(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'exits.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_exits(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Exit Log").run()

    # The Reason multiselect is the only multiselect in the main area.
    at.multiselect[0].set_value(["target"]).run()
    assert not at.exception
    table = at.dataframe[0].value
    assert set(table["reason"]) == {"target"}
    assert len(table) == 1
    assert set(table["message"]) == {"real-target-B"}


def test_exit_log_book_radio_filters_paper(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'exits.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_exits(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Exit Log").run()

    # Book radio is a main-area st.radio. The sidebar nav radio is at.sidebar.radio[0];
    # main-area radios are addressed via at.radio[...]. Drive it to "Paper".
    book_radio = at.radio[0]
    book_radio.set_value("Paper").run()
    assert not at.exception
    table = at.dataframe[0].value
    # Only the single is_paper=True event survives.
    assert len(table) == 1
    assert set(table["message"]) == {"real-stop-A"}
    assert set(bool(v) for v in table["is_paper"]) == {True}


def test_exit_log_account_facet_filters_paper_and_research(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'exits.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_exits(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Exit Log").run()

    # Account facet is the SECOND main-area radio (Book is at.radio[0], Account is [1]).
    # Drive it to "paper" -> only the intent-book exit (account="paper") survives.
    account_radio = at.radio[1]
    account_radio.set_value("paper").run()
    assert not at.exception
    table = at.dataframe[0].value
    assert len(table) == 1
    assert set(table["message"]) == {"real-stop-A"}
    assert set(table["account"]) == {"paper"}

    # ...and "research" shows only the two research-grid exits.
    at.radio[1].set_value("research").run()
    assert not at.exception
    table = at.dataframe[0].value
    assert len(table) == 2
    assert set(table["message"]) == {"real-target-B", "real-stop-C"}
    assert set(table["account"]) == {"research"}


def test_exit_log_no_match_state(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'exits.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    _seed_exits(url)
    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Exit Log").run()

    # Clear the reason multiselect -> no rows match -> friendly empty state, no table.
    at.multiselect[0].set_value([]).run()
    assert not at.exception
    assert len(at.dataframe) == 0
    infos = " ".join(str(getattr(el, "value", "")) for el in at.info)
    assert "match the filters" in infos
