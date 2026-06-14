from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def test_app_renders_on_empty_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SWING_DB_URL", f"sqlite:///{tmp_path / 'dash.sqlite'}")
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert any("Swing Screener" in t.value for t in at.title)
    # six tabs render
    assert len(at.tabs) == 6
