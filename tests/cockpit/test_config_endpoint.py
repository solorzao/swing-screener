"""GET /api/config -- the read-only configuration panel: sections/rows shape,
secrets never echoed, the read-only posture stated."""

from pathlib import Path

from fastapi.testclient import TestClient

from swing_screener.cockpit.api import create_app
from swing_screener.db.session import get_engine


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"
    get_engine(url)
    return TestClient(create_app(url, edge_dir=tmp_path))


def test_config_sections_and_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SWING_ACCOUNT_EQUITY", "1000")
    monkeypatch.setenv("SWING_RISK_PCT", "0.10")
    monkeypatch.setenv("SWING_EXECUTION_MODE", "paper")
    r = _client(tmp_path).get("/api/config")
    assert r.status_code == 200
    body = r.json()
    titles = [sec["title"] for sec in body["sections"]]
    assert titles[0] == "sizing" and "execution" in titles
    for sec in body["sections"]:
        assert sec["change_via"]
        for row in sec["rows"]:
            assert set(row) == {"key", "env", "value", "note"}
    sizing = {row["key"]: row for row in body["sections"][0]["rows"]}
    assert sizing["account equity"]["value"] == 1000.0
    assert sizing["resolved 1R"]["value"] == 100.0
    execution = {row["key"]: row["value"] for row in body["sections"][1]["rows"]}
    assert execution["execution mode"] == "paper"


def test_config_never_echoes_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-xyz")
    monkeypatch.setenv("DIGEST_TO", "someone@example.com")
    body = _client(tmp_path).get("/api/config").text
    assert "sk-secret-xyz" not in body
    assert "someone@example.com" not in body
    # The DB URL is deliberately absent too (the masthead chip carries the label).
    assert "sqlite:///" not in body
