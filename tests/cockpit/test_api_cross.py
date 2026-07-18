"""Cross-router API contracts (split from test_api.py): behaviors that
genuinely span two routers' wire paths."""

from pathlib import Path

from sqlalchemy.orm import Session

from tests.cockpit.conftest import (
    _analysis_row,
    _client_and_engine,
)


def test_stored_analysis_error_is_whitelisted_on_both_wire_paths(
    tmp_path: Path,
) -> None:
    """``AnalysisRequest.error`` reaches the wire on TWO paths (/api/analysis and
    the Zone E ticker); both route through the shared whitelist
    (``common._stored_error_detail``): the worker's two known-safe shapes pass
    verbatim, while a legacy raw-``str(exc)`` row (which can embed hosts, URLs,
    keys) serves the log pointer instead. The no-data arm is BOUNDED at 16 chars
    -- the writer's own ticker bound (``AnalysisRequest.ticker`` String(16)) --
    so a 17+-char token is not a shape the worker ever wrote and redacts (row
    DDD). Mutation-proof: serving the column raw puts 'secret-host' on the wire
    and both leak assertions fail; relaxing the bound back to ``\\S+`` serves
    DDD's long token verbatim and its pin fails."""
    long_token = "A" * 17  # one past the writer's String(16) ticker bound
    client, engine = _client_and_engine(tmp_path)
    with Session(engine) as s:
        s.add(_analysis_row(ticker="AAA", status="failed", error="no data for AAA"))
        s.add(_analysis_row(ticker="BBB", status="failed",
                            error="error (RuntimeError)"))
        s.add(_analysis_row(ticker="CCC", status="failed",
                            error="RuntimeError: https://secret-host.example?key=abc"))
        s.add(_analysis_row(ticker="DDD", status="failed",
                            error=f"no data for {long_token}"))
        s.commit()
    r = client.get("/api/analysis")
    errors = {row["ticker"]: row["error"] for row in r.json()["requests"]}
    assert errors == {"AAA": "no data for AAA", "BBB": "error (RuntimeError)",
                      "CCC": "error (details in log)",
                      "DDD": "error (details in log)"}
    assert "secret-host" not in r.text
    assert long_token not in r.text

    t = client.get("/api/ticker")
    details = {e["ticker"]: e["detail"] for e in t.json()["events"]
               if e["source"] == "analysis"}
    assert details == {"AAA": "no data for AAA", "BBB": "error (RuntimeError)",
                       "CCC": "error (details in log)",
                       "DDD": "error (details in log)"}
    assert "secret-host" not in t.text
    assert long_token not in t.text


