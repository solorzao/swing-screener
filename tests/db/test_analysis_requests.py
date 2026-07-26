from datetime import UTC, datetime

from sqlalchemy.orm import Session

from swing_screener.db.models import AnalysisRequest
from swing_screener.db.session import get_engine


def test_analysis_request_defaults_and_roundtrip():
    engine = get_engine("sqlite:///:memory:")
    with Session(engine) as s:
        s.add(AnalysisRequest(ticker="AMD", requested_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC)))
        s.commit()
        row = s.query(AnalysisRequest).one()
        assert row.ticker == "AMD"
        assert row.status == "queued"
        assert row.recipient == "" and row.summary == "" and row.chart_blob_keys == ""
        assert row.started_at is None and row.pdf_blob_key is None and row.error is None
