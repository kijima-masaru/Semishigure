"""Desktop launcher (Windows installer entry point, also usable elsewhere).

Starts the API/UI server on a free 127.0.0.1 port in a background thread and
shows the UI in an application window (pywebview; Edge WebView2 on Windows).
When pywebview is not available the UI opens in the default browser instead and
the process keeps serving until the console is closed or Ctrl-C.

The server is bound to 127.0.0.1 only: nothing is reachable from other hosts.
Runs in progress are stopped (every call BYE'd, REGISTER released, conf
overrides restored) when the window is closed, through the server's lifespan.
"""

from __future__ import annotations

import logging
import os
import socket
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

    def __init__(self, port: int, scenario_dir: Path, store_path: Path | None):
        import uvicorn

        from semishigure.api.app import create_app
        from semishigure.core.store import RunStore

        app = create_app(scenario_dir=scenario_dir, store=RunStore(store_path) if store_path else None)
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


def _open_window(url: str) -> bool:
    """Show the UI in an application window (pywebview / Edge WebView2).

    Returns False, after logging why, when no window can be shown: the caller
    falls back to the default browser. Any failure here must never take the
    whole program down silently.
    """
    try:
        import webview  # pywebview (BSD-3-Clause)
    except Exception as exc:  # noqa: BLE001 (ImportError, or a DLL/.NET loading error)
        log.warning("application window unavailable (%s): using the browser", exc)
        return False
    try:
        webview.create_window(f"蝉時雨 Semishigure {__version__}", url, width=1360, height=900, min_size=(900, 600), text_select=True)
        webview.start()  # returns when the window is closed
        return True
    except Exception:  # noqa: BLE001
        log.exception("application window failed: using the browser")
        return False


def run(window: bool = True, port: int | None = None, open_browser: bool = True) -> int:
    home = DEFAULT_DIR
    _log_setup(home)
    scenario_dir = Path(os.environ.get("SEMISHIGURE_SCENARIOS") or (home / "scenarios"))
    server = DesktopServer(port or _free_port(), scenario_dir, home / "runs.sqlite3")
    log.info("Semishigure %s starting on %s (home %s, python %s)", __version__, server.url, home, sys.executable)
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("server failed to start")
        _alert(f"Semishigure を起動できません: {exc}\n詳細: {home / 'desktop.log'}")
        return 1
    try:
        if window and _open_window(server.url):
            return 0
        if open_browser and not os.environ.get("SEMISHIGURE_NO_BROWSER"):
            webbrowser.open(server.url)
        print(f"Semishigure {__version__}: {server.url}  (Ctrl-C で終了)")
        if window and sys.platform == "win32":
            # no console to press Ctrl-C in: a message box is the stop button
            _info(f"画面をブラウザで開きました: {server.url}\n\nこの OK を押すと Semishigure を終了します。\n（アプリの窓が開けなかった理由は {home / 'desktop.log'} にあります）")
            return 0
        try:
            while server.thread.is_alive():
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
