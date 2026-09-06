"""Update check against GitHub Releases and, on the Windows desktop build, self-update.

The check reads the latest release of the project's repository. Installing is only
possible from the installed Windows layout (``<dir>\\Python\\pythonw.exe`` next to
``Semishigure.launch.pyw``): the installer is downloaded to ``~/.semishigure/updates``,
its SHA-256 is verified against the digest GitHub publishes for the asset, and a
detached copy of the bundled Python (``python -m semishigure.update --apply``) waits
for this process to exit, runs the installer silently into the same directory and
starts the application again, logging to ``~/.semishigure/updates/update.log``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from semishigure import __version__
from semishigure.secrets import DEFAULT_DIR

log = logging.getLogger("semishigure.update")

REPO = "kijima-masaru/Semishigure"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
CACHE_SECONDS = 1800
ERROR_CACHE_SECONDS = 300
_cache: dict = {}


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
        if m.end() != len(piece):  # "0-rc1": stop after the numeric prefix
            break
    return tuple(parts)


def _headers() -> dict[str, str]:
    return {"User-Agent": f"Semishigure/{__version__}", "Accept": "application/vnd.github+json"}


def fetch_latest(timeout: float = 8.0) -> dict:
    req = urllib.request.Request(API_LATEST, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https URL)
        return json.load(r)


def check(force: bool = False, fetch: Callable[[], dict] | None = None) -> dict:
    """Latest release vs the running version. Cached; never raises."""
    now = time.time()
    if not force and _cache and now < _cache["until"]:
        return _cache["result"]
    result = {"current": __version__, "latest": None, "newer": False, "url": None, "digest": None, "size": None, "html_url": None, "notes": "", "error": None, "checked_at": now}
    try:
        rel = (fetch or fetch_latest)()
        tag = str(rel.get("tag_name") or "")
        result["latest"] = tag.lstrip("vV")
        result["html_url"] = rel.get("html_url")
        result["notes"] = str(rel.get("body") or "")[:4000]
        for asset in rel.get("assets") or []:
            if str(asset.get("name") or "").lower().endswith("-setup.exe"):
                result["url"] = asset.get("browser_download_url")
                result["digest"] = asset.get("digest")
                result["size"] = asset.get("size")
                break
        result["newer"] = parse_version(tag) > parse_version(__version__)
        until = now + CACHE_SECONDS
    except Exception as exc:  # noqa: BLE001 (offline, rate limit, ...)
        result["error"] = str(exc)
        log.info("update check failed: %s", exc)
        until = now + ERROR_CACHE_SECONDS
    _cache.update(until=until, result=result)
    return result


def installed_layout() -> dict | None:
    """The installed Windows layout this process runs from, or None (dev checkout, other OS)."""
    if sys.platform != "win32":
        return None
    install_dir = Path(sys.executable).resolve().parent.parent
    launcher = install_dir / "Semishigure.launch.pyw"
    pythonw = install_dir / "Python" / "pythonw.exe"
    if launcher.is_file() and pythonw.is_file():
        return {"install_dir": install_dir, "launcher": launcher, "pythonw": pythonw}
    return None


def download(url: str | None, digest: str | None, dest: Path, opener: Callable = urllib.request.urlopen) -> Path:
    """Download the installer and verify its SHA-256 against the release digest."""
    if not url or not url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("the installer is not hosted at the project's releases")
    if not digest or not digest.lower().startswith("sha256:"):
        raise UpdateError("the release carries no checksum for the installer")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers=_headers())
    with opener(req, timeout=120) as r, open(tmp, "wb") as f:  # noqa: S310
        while chunk := r.read(1 << 16):
            f.write(chunk)
            h.update(chunk)
    if h.hexdigest() != digest.split(":", 1)[1].strip().lower():
        tmp.unlink(missing_ok=True)
        raise UpdateError("checksum mismatch: the downloaded installer was discarded")
    tmp.replace(dest)
    return dest


def _install_mode(install_dir: Path) -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    return "/currentuser" if local and str(install_dir).lower().startswith(local.lower()) else "/allusers"


def installer_params(install_dir: Path) -> str:
    """NSIS command line: silent, same mode and directory as the running installation."""
    return f'/S {_install_mode(install_dir)} /INSTDIR="{install_dir}"'


def start_installer(setup: Path, layout: dict, pid: int | None = None) -> Path:
    """Spawn a detached copy of our own Python that applies the update once this process is gone.

    The child (``python -m semishigure.update --apply ...``) waits for this PID to exit, runs
    the installer silently into the same directory and starts the application again. It
    writes what it does to ``update.log`` next to the installer.
    """
    log_path = setup.parent / "update.log"
    install_dir = Path(layout["install_dir"])
    python = install_dir / "Python" / "python.exe"
    if not python.is_file():
        python = Path(layout["pythonw"])
    args = [str(python), "-m", "semishigure.update", "--apply", str(setup), "--pid", str(pid or os.getpid()), "--install-dir", str(install_dir), "--launcher", str(layout["launcher"]), "--pythonw", str(layout["pythonw"]), "--log", str(log_path)]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    kwargs = {"close_fds": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(setup.parent)}
    try:
        subprocess.Popen(args, creationflags=flags | breakaway, **kwargs)  # noqa: S603
    except OSError:
        subprocess.Popen(args, creationflags=flags, **kwargs)  # noqa: S603 (not in a job / no breakaway right)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} scheduled: {setup} after exit of pid {pid or os.getpid()} (mode {_install_mode(install_dir)})\n")
    log.info("update: installer %s scheduled after exit of pid %s", setup, pid or os.getpid())
    return log_path


# ---- the detached updater (runs in its own process; no imports beyond the stdlib) ----------


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """True once ``pid`` is gone (Windows: wait on the process handle; else poll)."""
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True  # already gone (or not ours)
        try:
            return k32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            k32.CloseHandle(handle)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        try:  # Linux: a zombie (exited, not yet reaped by its parent) counts as gone
            if pathlib_state(pid) == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.5)
    return False


def pathlib_state(pid: int) -> str:
    with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
        return f.read().rsplit(")", 1)[1].split()[0]


def _run_installer(setup: Path, params: str) -> int:
    """Run the installer and return its exit code.

    Windows: ShellExecuteEx, so an installer that asks for elevation gets the UAC prompt
    instead of a plain 'elevation required' failure.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
        info.lpVerb = "open"
        info.lpFile = str(setup)
        info.lpParameters = params
        info.lpDirectory = str(setup.parent)
        info.nShow = 1  # SW_SHOWNORMAL (the installer itself is silent)
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
            raise OSError(ctypes.get_last_error() or ctypes.GetLastError(), "ShellExecuteEx failed (UAC declined?)")
        if not info.hProcess:
            return 0
        k32 = ctypes.windll.kernel32
        k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        k32.CloseHandle(info.hProcess)
        return int(code.value)
    return subprocess.call([str(setup), *params.split()])  # noqa: S603 (tests)


def apply_update(setup: Path, pid: int, install_dir: Path, launcher: Path, pythonw: Path, log_path: Path, wait_timeout: float = 600.0) -> int:
    """Body of the detached updater: wait, install, restart. Returns the installer's exit code."""

    def note(msg: str) -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

    note(f"updater started (pid {os.getpid()}): waiting for pid {pid} to exit")
    if not _wait_for_exit(pid, wait_timeout):
        note(f"pid {pid} is still running after {wait_timeout:.0f}s: giving up")
        return 1
    time.sleep(1.0)  # let file handles close
    params = installer_params(install_dir)
    note(f"running installer: {setup} {params}")
    try:
        rc = _run_installer(setup, params)
        note(f"installer exit code {rc}")
    except OSError as exc:
        note(f"installer could not be started: {exc}")
        rc = 1
    note(f"restarting the application: {pythonw} {launcher}")
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(pythonw), str(launcher)], creationflags=flags, close_fds=True, cwd=str(install_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
        note("done")
    except OSError as exc:
        note(f"restart failed: {exc}")
    return rc


def apply_main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m semishigure.update", description="apply a downloaded Semishigure installer after the application exits")
    ap.add_argument("--apply", required=True, help="path of the downloaded setup.exe")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--install-dir", required=True)
    ap.add_argument("--launcher", required=True)
    ap.add_argument("--pythonw", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--wait-timeout", type=float, default=600.0)
    a = ap.parse_args(argv)
    return apply_update(Path(a.apply), a.pid, Path(a.install_dir), Path(a.launcher), Path(a.pythonw), Path(a.log), a.wait_timeout)


def install_latest(result: dict) -> dict:
    layout = installed_layout()
    if layout is None:
        raise UpdateError("not_supported")
    if not result.get("newer") or not result.get("url"):
        raise UpdateError("no newer version")
    dest = DEFAULT_DIR / "updates" / Path(str(result["url"])).name
    download(result["url"], result.get("digest"), dest)
    log_path = start_installer(dest, layout)
    return {"setup": str(dest), "log": str(log_path), "version": result.get("latest")}


if __name__ == "__main__":
    sys.exit(apply_main())
