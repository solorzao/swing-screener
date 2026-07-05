"""Double-clickable cockpit launcher: ``pythonw -m swing_screener.cockpit``.

Stage 2 of the two-stage startup (docs/plans/2026-07-05-desktop-ui-design.md): uvicorn
serves the API + built frontend on a localhost port in a daemon thread, then either a
pywebview window or -- under ``--browser`` or without pywebview installed -- the default
browser opens on it.

Two constraints, stated as contract:

* pywebview is imported LAZILY inside the window path only. ``--browser`` mode and every
  test in tests/cockpit/test_launcher.py must run on a box without the desktop extra;
  availability is sniffed with ``find_spec`` (which never executes the module).
* One instance only, politely: a port-file at ``%LOCALAPPDATA%/swing-screener/
  cockpit.port`` records the live port. A second launch probes it (~1 s) and defers
  with "already running" / exit 0; a stale or garbage file is simply overwritten --
  the probe, not the file, is the source of truth.
"""

import argparse
import contextlib
import importlib.util
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

DEFAULT_DB_URL = "sqlite:///local.db"
WINDOW_TITLE = "Swing Screener"
WINDOW_SIZE = (1480, 960)


def _pick_free_port() -> int:
    """An OS-assigned free localhost port (the bind-to-0 trick, released on close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """``--db`` (default: SWING_DB_URL, then local sqlite), ``--port`` (default: pick
    free), ``--browser``. The env default matches ``settings.py``: a user who
    configured SWING_DB_URL and double-clicks must land on THEIR database, not on a
    silently created empty local.db. Read at parse time, not import time (test seam).
    """
    parser = argparse.ArgumentParser(
        prog="python -m swing_screener.cockpit",
        description="Launch the swing-screener cockpit (desktop window or browser tab).",
    )
    parser.add_argument(
        "--db", default=os.environ.get("SWING_DB_URL", DEFAULT_DB_URL),
        help="database URL (default: $SWING_DB_URL, else sqlite:///local.db)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="port to serve on (default: a free port)"
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="open the default browser instead of a pywebview window",
    )
    return parser.parse_args(argv)


def _choose_mode(*, browser_flag: bool, webview_available: bool) -> Literal["window", "browser"]:
    """Pure mode decision -- importing pywebview is main()'s job, in the window path only."""
    if browser_flag or not webview_available:
        return "browser"
    return "window"


def _port_file_path() -> Path:
    """``%LOCALAPPDATA%/swing-screener/cockpit.port`` (XDG-ish fallback off-Windows)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "swing-screener" / "cockpit.port"


def _already_running(port_file: Path, probe: Callable[[int], bool]) -> int | None:
    """The live instance's port, or None. The PROBE decides -- a port-file alone proves
    nothing (crashes don't clean up), so missing/garbage/stale files all read as
    'not running' and get overwritten by the caller."""
    try:
        port = int(port_file.read_text().strip())
    except (OSError, ValueError):
        return None
    return port if probe(port) else None


def _probe_health(port: int, timeout_s: float = 1.0) -> bool:
    """True iff ``/api/health`` on localhost:<port> answers OK within the timeout."""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 (fixed localhost URL)
            return bool(200 <= resp.status < 300)
    except Exception:
        return False


def _wait_until_responsive(
    probe: Callable[[], bool],
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.2,
    server_alive: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``probe`` until it succeeds or ``timeout_s`` elapses (uvicorn needs a
    moment between thread start and a listening socket).

    ``server_alive`` (the server thread's ``is_alive``) short-circuits the wait: a
    uvicorn bind failure raises SystemExit INSIDE the daemon thread, which threading
    swallows -- without this check the loop would poll a corpse for the full timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server_alive is not None and not server_alive():
            return False  # the server died; no probe will ever succeed
        if probe():
            return True
        time.sleep(interval_s)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Thin glue over the tested parts: serve, wait, open, clean up the port-file."""
    import uvicorn

    from swing_screener.cockpit.api import create_app

    args = _parse_args(argv)
    port_file = _port_file_path()
    live_port = _already_running(port_file, _probe_health)
    if live_port is not None:
        print(f"cockpit already running on http://127.0.0.1:{live_port}")
        return 0

    port = args.port if args.port is not None else _pick_free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(args.db), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="cockpit-uvicorn", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    if not _wait_until_responsive(lambda: _probe_health(port), server_alive=thread.is_alive):
        print(f"cockpit server failed to answer on {url} within 10s", file=sys.stderr)
        return 1

    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port))
    try:
        mode = _choose_mode(
            browser_flag=args.browser,
            webview_available=importlib.util.find_spec("webview") is not None,
        )
        if mode == "window":
            import webview  # lazy: only the window path may require the desktop extra

            webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])
            webview.start()  # blocks until the window closes
        else:
            webbrowser.open(url)
            thread.join()  # serve until Ctrl+C -- the browser tab owns no lifecycle
    finally:
        # Best-effort: a leftover file is harmless (the probe disowns stale files).
        with contextlib.suppress(OSError):
            port_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
