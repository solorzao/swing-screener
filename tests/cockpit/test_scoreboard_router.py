"""The /api/stats/scoreboard endpoint: a thin HTTP wrapper over the pure
``build_scoreboard`` aggregation. Read-only (no cockpit-header guard, like
/api/stats/performance); the endpoint returns all four cards with honest zeros
on an empty DB, so the mounted-route shape is provable without seeding."""

from pathlib import Path

from fastapi.testclient import TestClient

from swing_screener.cockpit.api import create_app
from swing_screener.db.session import get_engine


def _db_url(tmp_path: Path) -> str:
    # as_posix(): backslashes in a sqlite URL are asking for escape trouble.
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def _client(tmp_path: Path) -> TestClient:
    url = _db_url(tmp_path)
    get_engine(url)  # seed the file + schema; the app builds its OWN engine from the URL
    return TestClient(create_app(url, edge_dir=tmp_path))


_BOOKS = {"manual_equity", "robinhood", "live", "paper"}


def test_scoreboard_endpoint_shape(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/stats/scoreboard")
    assert r.status_code == 200
    body = r.json()
    books = {c["book"] for c in body["cards"]}
    assert books == _BOOKS
    assert body["combined"]["books"] == ["manual_equity", "live"]
    assert body["combined"]["unit"] == "R"


def test_scoreboard_window_param_passes_through(tmp_path: Path) -> None:
    """The Literal-typed ``window`` param is accepted and threaded to
    ``build_scoreboard``: a valid value returns the same four-card shape, and an
    unknown value is FastAPI's 422 via the Literal, never a silent fallback."""
    client = _client(tmp_path)
    r = client.get("/api/stats/scoreboard?window=90")
    assert r.status_code == 200
    assert {c["book"] for c in r.json()["cards"]} == _BOOKS
    assert client.get("/api/stats/scoreboard?window=bogus").status_code == 422
