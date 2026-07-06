"""Double-clickable cockpit launcher: ``pythonw -m swing_screener.cockpit``.

Stage 2 of the two-stage startup (docs/plans/2026-07-05-desktop-ui-design.md): uvicorn
serves the API + built frontend on a localhost port in a daemon thread, then either a
pywebview window or -- under ``--browser`` or without pywebview installed -- the default
browser opens on it.

Three constraints, stated as contract:

* pywebview is imported LAZILY inside the window path only. ``--browser`` mode and every
  test in tests/cockpit/test_launcher.py must run on a box without the desktop extra;
  availability is sniffed with ``find_spec`` (which never executes the module).
* One instance only, politely: a port-file at ``%LOCALAPPDATA%/swing-screener/
  cockpit.port`` records the live port. A second launch probes it (~1 s) and defers
  with "already running" / exit 0; a stale or garbage file is simply overwritten --
  the probe, not the file, is the source of truth.
* Failure is VISIBLE: under ``pythonw`` (the Start-menu shortcut) there is no console,
  so a startup failure that only printed to stderr would look like nothing happened.
  ``_report_startup_failure`` routes it to stderr when a console exists, else to a
  WinAPI message box -- carrying a leak-safe one-liner (exception class only, never
  the DB URL or a path, same posture as ``api._down_summary``).
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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

# True once _ensure_streams replaced a None stderr: the process is headless (pythonw)
# even though sys.stderr is no longer None -- the failure reporter must keep routing
# to the message box, not print into devnull.
_shimmed_headless = False


def _ensure_streams() -> None:
    """Bind ``sys.stdout``/``sys.stderr`` to devnull when the interpreter has none.

    Under ``pythonw`` both are ``None``: any bare ``print`` (the already-running
    notice, library writes) raises, and uvicorn's logging setup ``ValueError``\\ s
    against a ``None`` stream -- the 2026-07-05 "startup failed (ValueError)" field
    report from the Start-menu shortcut. Real console streams are left untouched;
    shimming records ``_shimmed_headless`` so the console probe stays truthful."""
    global _shimmed_headless
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 -- lives for the process
        _shimmed_headless = True
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 -- lives for the process


def _uvicorn_server(app: "FastAPI", port: int) -> "uvicorn.Server":
    """A uvicorn server with uvicorn's OWN logging config disabled (``log_config=None``).

    The default config dictConfigs handlers onto ``sys.stderr`` at ``Config()``
    construction time -- ``ValueError`` before the server ever starts when the stream
    is ``None`` (pythonw). A desktop app has no audience for uvicorn's banner or
    access log either way."""
    import uvicorn

    return uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning", log_config=None, access_log=False,
    ))


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


def _startup_failure_reason(exc: Exception) -> str:
    """One leak-safe line for the box/stderr: the exception CLASS only -- driver and
    OS messages can embed the DB URL or file paths (same posture as api._down_summary)."""
    return f"startup failed ({type(exc).__name__})"


def _windows_message_box(reason: str) -> None:
    """MB_ICONERROR modal box -- the only surface a console-less pythonw process has.
    Platform-guarded so non-Windows boxes (CI, a stray mac) degrade to a no-op:
    pythonw itself only exists on Windows, so the guard never hides a real user."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, reason, "Swing Screener Cockpit", 0x10)


def _report_startup_failure(
    reason: str,
    *,
    has_console: bool | None = None,
    messagebox: Callable[[str], None] | None = None,
) -> None:
    """Route a startup failure somewhere a human will actually SEE it.

    Under ``pythonw`` (the Start-menu shortcut) there is no console and
    ``sys.stderr`` is ``None`` -- a print would land nowhere and the failed
    double-click would look like nothing happened. So: stderr when a console
    exists, a message box when not. ``reason`` must already be leak-safe (build
    it with ``_startup_failure_reason``). ``has_console`` and ``messagebox`` are
    test seams for the console probe and the WinAPI call; the defaults probe
    ``sys.stderr`` (``None`` under pythonw) and open the real box."""
    console = (has_console if has_console is not None
               else sys.stderr is not None and not _shimmed_headless)
    if console:
        print(f"swing-screener cockpit: {reason}", file=sys.stderr)
        return
    (messagebox if messagebox is not None else _windows_message_box)(reason)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``_run`` wrapped in the visible-failure net (module docstring,
    constraint 3). Any exception escaping startup or the serve loop is reported
    class-name-only and becomes exit 1; KeyboardInterrupt still propagates --
    Ctrl+C in ``--browser`` mode is the user stopping the server, not a failure."""
    _ensure_streams()  # before anything can print: pythonw ships None streams
    try:
        return _run(argv)
    except Exception as exc:
        _report_startup_failure(_startup_failure_reason(exc))
        return 1


def _run(argv: Sequence[str] | None) -> int:
    """Thin glue over the tested parts: serve, wait, open, clean up the port-file."""
    from swing_screener.cockpit.api import create_app

    args = _parse_args(argv)
    port_file = _port_file_path()
    live_port = _already_running(port_file, _probe_health)
    if live_port is not None:
        print(f"cockpit already running on http://127.0.0.1:{live_port}")
        return 0

    port = args.port if args.port is not None else _pick_free_port()
    server = _uvicorn_server(create_app(args.db), port)
    thread = threading.Thread(target=server.run, name="cockpit-uvicorn", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    if not _wait_until_responsive(lambda: _probe_health(port), server_alive=thread.is_alive):
        # Port not URL in the reason: the string also feeds the pythonw message box,
        # which carries no URLs or paths by contract.
        _report_startup_failure(f"server did not answer on port {port} within 10s")
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

            with contextlib.suppress(Exception):  # cosmetic: own taskbar identity,
                import ctypes                     # not python.exe's

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "SwingScreener.Cockpit")
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
