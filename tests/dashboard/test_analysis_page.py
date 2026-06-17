from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from swing_screener.db import repo
from swing_screener.db.models import AnalysisRequest
from swing_screener.db.session import get_engine

APP = str(Path(__file__).parents[2] / "src" / "swing_screener" / "dashboard" / "app.py")


def test_request_form_creates_queued_row(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'analysis.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    # ensure tables exist on an empty DB before the app touches it
    get_engine(url)

    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Deep Analysis").run()
    at.text_input[0].set_value("amd").run()
    # the form submit button is the first button on the page
    at.button[0].click().run()
    assert not at.exception

    engine = get_engine(url)
    with Session(engine) as s:
        rows = repo.list_analysis_requests(s)
        assert len(rows) == 1
        assert rows[0].ticker == "AMD"
        assert rows[0].status == "queued"


def test_done_request_shows_summary_and_pdf(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'analysis.sqlite'}"
    monkeypatch.setenv("SWING_DB_URL", url)
    # blob disabled -> PDF resolves from a real local path
    pdf_path = tmp_path / "amd_report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    engine = get_engine(url)
    with Session(engine) as s:
        s.add(AnalysisRequest(
            ticker="AMD", requested_at=datetime.now(), status="done",
            summary="AMD is consolidating above the 50-day; watch 96 for entry.",
            pdf_blob_key=str(pdf_path), chart_blob_keys=""))
        s.commit()

    at = AppTest.from_file(APP).run()
    at.sidebar.radio[0].set_value("Deep Analysis").run()
    assert not at.exception

    rendered = " ".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "AMD" in rendered
    assert "consolidating" in rendered
    # a Download PDF button is offered for the done report (no dedicated AppTest
    # accessor for download_button in this Streamlit version -> reach it via .get)
    dl = list(at.get("download_button"))
    assert any("Download PDF" in str(getattr(b, "label", "")) for b in dl)
