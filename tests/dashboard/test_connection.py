"""Connection-guard behavior for the dashboard's top-level render()."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


@pytest.fixture(autouse=True)
def _clear_caches():
    # @st.cache_resource (engine) and @st.cache_data (the connectivity check) persist
    # across reruns within the AppTest process, which could leak between tests or mask
    # the monkeypatched DB-down. Clear BOTH around every test.
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


def _raise(_url: str):
    raise RuntimeError("boom")


def test_db_down_shows_friendly_card_and_red_chip(tmp_path, monkeypatch):
    # No real broken server needed: make get_engine raise so the connectivity
    # check fails. The app must NOT crash (no uncaught exception), must surface a
    # friendly "Can't reach the database." error, and the sidebar chip must show
    # the red indicator. We patch the SOURCE (db.session.get_engine), not
    # app.get_engine: AppTest re-execs app.py each run, and its
    # `from swing_screener.db.session import get_engine` line re-binds the name
    # in the app module — so a patch on app.get_engine is clobbered, while a
    # patch on the source persists through the re-import.
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    monkeypatch.setattr("swing_screener.db.session.get_engine", _raise)
    at = AppTest.from_file(APP).run()

    assert not at.exception
    assert any("Can't reach the database" in str(e.value) for e in at.error)
    # The chip is a sidebar caption; on failure it shows the red indicator.
    chip_text = " ".join(str(c.value) for c in at.sidebar.caption)
    assert "🔴" in chip_text


def test_healthy_db_shows_green_chip(tmp_path, monkeypatch):
    # The seeded/healthy sqlite path must connect cleanly and show the green chip.
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()

    assert not at.exception
    chip_text = " ".join(str(c.value) for c in at.sidebar.caption)
    assert "🟢" in chip_text
    # No DB-down error card on the healthy path.
    assert not any("Can't reach the database" in str(e.value) for e in at.error)


def test_healthy_db_stays_green_across_reruns(tmp_path, monkeypatch):
    # The connectivity check is cached (short TTL) so a burst of reruns doesn't reconnect.
    # A second rerun must reuse the cached result and still render green without crashing.
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()
    at.run()  # second rerun -> the cached connectivity result is reused
    assert not at.exception
    chip_text = " ".join(str(c.value) for c in at.sidebar.caption)
    assert "🔴" not in chip_text and "🟢" in chip_text
