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


def test_config_execute_play_types_row(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SWING_EXECUTE_PLAY_TYPES", "reversal")
    body = _client(tmp_path).get("/api/config").json()
    execution = {row["key"]: row for row in body["sections"][1]["rows"]}
    row = execution["execute play types"]
    assert row["env"] == "SWING_EXECUTE_PLAY_TYPES"
    assert row["value"] == ["reversal"]


def test_config_execute_play_types_row_name_matches_the_cockpit_helpterm(
        tmp_path: Path, monkeypatch) -> None:
    """SafetyScreen's ``CfgKey`` hangs the execution-scope glossary term off THIS
    row's NAME, so renaming the row here would silently drop the help affordance
    (an unmatched key renders plain text -- never an error). Same rot class the
    glossary citation guard covers, pinned from the side that would move."""
    monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    body = _client(tmp_path).get("/api/config").json()
    keys = {row["key"] for sec in body["sections"] for row in sec["rows"]}
    tsx = (Path(__file__).resolve().parents[2]
           / "cockpit-ui" / "src" / "screens" / "SafetyScreen.tsx").read_text(encoding="utf-8")
    assert "execute play types" in keys
    assert "name === 'execute play types'" in tsx


def test_config_execute_play_types_row_shows_resolved_value(
        tmp_path: Path, monkeypatch) -> None:
    # The row renders the RESOLVED value, not the raw env: the invalid member is
    # already dropped by the fail-closed parse.
    monkeypatch.setenv("SWING_EXECUTE_PLAY_TYPES", "reversal,junk")
    body = _client(tmp_path).get("/api/config").json()
    execution = {row["key"]: row["value"] for row in body["sections"][1]["rows"]}
    assert execution["execute play types"] == ["reversal"]


def test_config_execute_play_types_unset_renders_null(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SWING_EXECUTE_PLAY_TYPES", raising=False)
    body = _client(tmp_path).get("/api/config").json()
    execution = {row["key"]: row["value"] for row in body["sections"][1]["rows"]}
    assert execution["execute play types"] is None  # unset = all play types


def test_config_never_echoes_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-xyz")
    monkeypatch.setenv("DIGEST_TO", "someone@example.com")
    body = _client(tmp_path).get("/api/config").text
    assert "sk-secret-xyz" not in body
    assert "someone@example.com" not in body
    # The DB URL is deliberately absent too (the masthead chip carries the label).
    assert "sqlite:///" not in body
