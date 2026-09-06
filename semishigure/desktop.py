"""Desktop launcher (Windows installer entry point, also usable elsewhere).

Starts the API/UI server on a free 127.0.0.1 port in a background thread and
shows the UI in an application window: Microsoft Edge (or Chrome) in "app"
mode with its own profile directory, which is a plain browser process that
lives exactly as long as the window. Nothing runs inside our process for the
window, so a browser problem cannot take the app down. Without Edge/Chrome the
UI opens in the default browser instead and the process keeps serving until
the console is closed or Ctrl-C (a message box on Windows).

The server is bound to 127.0.0.1 only: nothing is reachable from other hosts.
Runs in progress are stopped (every call BYE'd, REGISTER released, conf
overrides restored) when the window is closed, through the server's lifespan.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from semishigure import __version__
from semishigure.secrets import DEFAULT_DIR

log = logging.getLogger("semishigure.desktop")


def _free_port(preferred: int = 8080) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free TCP port on 127.0.0.1")


class DesktopServer:
    """uvicorn in a daemon thread; ``stop()`` runs the app's shutdown (stops runs)."""

    def __init__(self, port: int, scenario_dir: Path, store_path: Path | None, request_exit=None):
        import uvicorn

        from semishigure.api.app import create_app
        from semishigure.core.store import RunStore

        app = create_app(scenario_dir=scenario_dir, store=RunStore(store_path) if store_path else None, desktop=True, request_exit=request_exit)
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="semishigure-server", daemon=True)
        self.url = f"http://127.0.0.1:{port}/"

    def start(self, timeout: float = 20.0) -> None:
        self.thread.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                with urllib.request.urlopen(self.url + "api/state", timeout=1) as r:  # noqa: S310 (localhost)
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        raise RuntimeError("the server did not start")

    def stop(self, timeout: float = 30.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def _log_setup(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.FileHandler(home / "desktop.log", encoding="utf-8")])


def _app_browser() -> str | None:
    """Path of a Chromium-based browser that supports ``--app`` (Edge, Chrome), or None."""
    env = os.environ.get
    if sys.platform == "win32":
        import winreg

        for exe in ("msedge.exe", "chrome.exe"):
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        path, _ = winreg.QueryValueEx(k, "")
                    if path and os.path.isfile(path):
                        return path
                except OSError:
                    continue
        candidates = [
            os.path.join(env("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(env("ProgramFiles", r"C:\Program Files"), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(env("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(env("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [shutil.which(n) or "" for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def _open_window(url: str, home: Path, stop: threading.Event | None = None) -> bool:
    """Show the UI in an application window and return when it is closed.

    Returns False, after logging why, when no window can be shown: the caller
    falls back to the default browser. The window is Edge/Chrome in ``--app``
    mode with a private profile under ``home``: a separate process, so a
    failure there never takes this process down.
    """
    browser = _app_browser()
    if not browser:
        log.warning("no Edge/Chrome for the application window: using the default browser")
        return False
    profile = home / "window-profile"
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-sync",
        "--window-size=1360,900",
    ]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.exception("could not start %s: using the default browser", browser)
        return False
    log.info("application window: %s (pid %s)", browser, proc.pid)
    # the browser process lives as long as its window (own profile => no sharing
    # with an already running Edge/Chrome); an immediate exit means it failed
    try:
        rc = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        while proc.poll() is None:
            if stop is not None and stop.wait(0.5):
                log.info("closing the application window (exit requested: update)")
                proc.terminate()
                break
        log.info("application window closed")
        return True
    log.warning("application window exited immediately (code %s): using the default browser", rc)
    return False


def run(window: bool = True, port: int | None = None, open_browser: bool = True) -> int:
    home = DEFAULT_DIR
    _log_setup(home)
    scenario_dir = Path(os.environ.get("SEMISHIGURE_SCENARIOS") or (home / "scenarios"))
    stop = threading.Event()  # set by the server when an update was started: close the window, exit
    server = DesktopServer(port or _free_port(), scenario_dir, home / "runs.sqlite3", request_exit=stop.set)
    log.info("Semishigure %s starting on %s (home %s, python %s)", __version__, server.url, home, sys.executable)
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("server failed to start")
        _alert(f"Semishigure を起動できません: {exc}\n詳細: {home / 'desktop.log'}")
        return 1
    try:
        if window and _open_window(server.url, home, stop):
            return 0
        if open_browser and not os.environ.get("SEMISHIGURE_NO_BROWSER"):
            webbrowser.open(server.url)
        print(f"Semishigure {__version__}: {server.url}  (Ctrl-C で終了)")
        if window and sys.platform == "win32":
            # no console to press Ctrl-C in: a message box is the stop button
            _info(f"画面を既定のブラウザで開きました: {server.url}\n\nこの OK を押すと Semishigure を終了します。\n（Microsoft Edge か Google Chrome があればアプリ窓で開きます。詳細: {home / 'desktop.log'}）")
            return 0
        try:
            while server.thread.is_alive() and not stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        log.info("shutting down")
        server.stop()


def _message_box(message: str, flags: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Semishigure", flags)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _alert(message: str) -> None:
    if not _message_box(message, 0x10):  # MB_ICONERROR
        print(message, file=sys.stderr)


def _info(message: str) -> None:
    if not _message_box(message, 0x40):  # MB_ICONINFORMATION
        print(message)


def _report_crash() -> None:
    """Last resort for the GUI entry point: record and show what went wrong."""
    text = traceback.format_exc()
    try:
        DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
        with open(DEFAULT_DIR / "desktop.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} CRASH\n{text}\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    _alert(f"Semishigure が起動できませんでした。\n\n{text.strip().splitlines()[-1]}\n\n詳細: {DEFAULT_DIR / 'desktop.log'}")


def main() -> int:
    """GUI entry point (application window); never exits silently on an error."""
    try:
        return run(window=True)
    except Exception:  # noqa: BLE001
        _report_crash()
        return 1


def main_console() -> int:
    """Console entry point: server + default browser, Ctrl-C to stop."""
    logging.getLogger().addHandler(logging.StreamHandler())
    return run(window=False)


if __name__ == "__main__":
    sys.exit(main())
