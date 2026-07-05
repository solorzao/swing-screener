"""Launcher contract: a free port is genuinely bindable, the app serves the built
frontend at ``/`` (API routes still winning), a missing build is a friendly hint,
pywebview is never imported outside the window path, and a second launch defers
to a live instance instead of racing it."""

import importlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swing_screener.cockpit.__main__ import (
    _already_running,
    _choose_mode,
    _parse_args,
    _pick_free_port,
    _wait_until_responsive,
)
from swing_screener.cockpit.api import create_app


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cockpit.db').as_posix()}"


def test_root_serves_index_html_when_frontend_is_built(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html><body>cockpit stub</body></html>")
    client = TestClient(create_app(_db_url(tmp_path), edge_dir=tmp_path, static_dir=static))
    r = client.get("/")
    assert r.status_code == 200
    assert "cockpit stub" in r.text
    assert r.headers["content-type"].startswith("text/html")
    # API routes are registered BEFORE the static mount, so they still win.
    health = client.get("/api/health")
    assert health.status_code == 200
    assert "connected" in health.json()


def test_root_without_frontend_build_is_a_friendly_hint(tmp_path: Path) -> None:
    # Task 6 builds the real frontend; until then (and on a broken build) `/`
    # must answer with a pointer, never a 404 shrug or a traceback.
    client = TestClient(
        create_app(_db_url(tmp_path), edge_dir=tmp_path, static_dir=tmp_path / "absent")
    )
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["detail"].startswith("frontend not built")


def test_pick_free_port_returns_a_bindable_localhost_port() -> None:
    port = _pick_free_port()
    assert 1 <= port <= 65535
    # The proof is the bind: the port must be genuinely free right after picking.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_parse_args_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWING_DB_URL", raising=False)  # isolate from the dev box's env
    defaults = _parse_args([])
    assert defaults.db == "sqlite:///local.db"
    assert defaults.port is None  # None means: pick a free port at launch
    assert defaults.browser is False
    explicit = _parse_args(["--db", "sqlite:///x.db", "--port", "8901", "--browser"])
    assert (explicit.db, explicit.port, explicit.browser) == ("sqlite:///x.db", 8901, True)


def test_db_default_honors_swing_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rest of the system reads SWING_DB_URL (settings.py); the double-click
    # launch must follow it too, or a configured user silently gets a fresh empty
    # local.db that looks healthy.
    monkeypatch.setenv("SWING_DB_URL", "sqlite:///from-env.db")
    assert _parse_args([]).db == "sqlite:///from-env.db"
    assert _parse_args(["--db", "sqlite:///cli.db"]).db == "sqlite:///cli.db"  # CLI wins


def test_choose_mode_prefers_window_but_never_requires_pywebview() -> None:
    assert _choose_mode(browser_flag=False, webview_available=True) == "window"
    # --browser wins even with pywebview installed: the user asked for a tab.
    assert _choose_mode(browser_flag=True, webview_available=True) == "browser"
    # No pywebview is a graceful fallback, not a crash.
    assert _choose_mode(browser_flag=False, webview_available=False) == "browser"


def test_importing_the_launcher_never_imports_pywebview() -> None:
    # pywebview must load lazily inside the window path ONLY: --browser mode and
    # these very tests must work on a box without the desktop extra. Re-executing
    # the module top-level proves import time stays webview-free.
    sys.modules.pop("webview", None)
    importlib.reload(importlib.import_module("swing_screener.cockpit.__main__"))
    assert "webview" not in sys.modules


def test_already_running_returns_port_of_live_instance(tmp_path: Path) -> None:
    port_file = tmp_path / "cockpit.port"
    port_file.write_text("8901")
    probed: list[int] = []

    def probe(port: int) -> bool:
        probed.append(port)
        return True

    assert _already_running(port_file, probe) == 8901
    assert probed == [8901]  # the health probe targets the recorded port


def test_already_running_treats_missing_stale_or_garbage_port_file_as_not_running(
    tmp_path: Path,
) -> None:
    port_file = tmp_path / "cockpit.port"
    assert _already_running(port_file, lambda _p: True) is None  # no file
    port_file.write_text("not-a-port")
    assert _already_running(port_file, lambda _p: True) is None  # garbage content
    port_file.write_text("8901")
    assert _already_running(port_file, lambda _p: False) is None  # dead server: stale file


def test_wait_until_responsive_polls_until_the_probe_succeeds() -> None:
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return len(calls) >= 3

    assert _wait_until_responsive(probe, timeout_s=5.0, interval_s=0.0) is True
    assert len(calls) == 3  # stops at first success, no extra polls


def test_wait_until_responsive_gives_up_after_the_deadline() -> None:
    assert _wait_until_responsive(lambda: False, timeout_s=0.05, interval_s=0.01) is False


def test_wait_until_responsive_fails_fast_when_the_server_thread_dies() -> None:
    # A uvicorn bind failure raises SystemExit INSIDE the daemon thread, which
    # threading swallows -- the wait must notice the corpse, not poll it for 10s.
    corpse = threading.Thread(target=lambda: None)
    corpse.start()
    corpse.join()
    started = time.monotonic()
    ok = _wait_until_responsive(
        lambda: False, timeout_s=10.0, interval_s=0.01, server_alive=corpse.is_alive
    )
    assert ok is False
    assert time.monotonic() - started < 2.0  # bailed immediately, far under timeout_s
