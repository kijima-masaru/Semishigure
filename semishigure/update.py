"""Update check against GitHub Releases and, on the Windows desktop build, self-update.

The check reads the latest release of the project's repository. Installing is only
possible from the installed Windows layout (``<dir>\\Python\\pythonw.exe`` next to
``Semishigure.launch.pyw``): the installer is downloaded to ``~/.semishigure/updates``,
its SHA-256 is verified against the digest GitHub publishes for the asset, and a
detached PowerShell script waits for this process to exit, runs the installer
silently into the same directory and starts the application again.
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


def updater_script(setup: Path, layout: dict, pid: int) -> str:
    install_dir = Path(layout["install_dir"])
    local = os.environ.get("LOCALAPPDATA", "")
    mode = "/currentuser" if local and str(install_dir).lower().startswith(local.lower()) else "/allusers"
    return (
        '$ErrorActionPreference = "Continue"\n'
        f"$target = {pid}\n"
        "while (Get-Process -Id $target -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 500 }\n"
        f'Start-Process -FilePath "{setup}" -ArgumentList \'/S {mode} /INSTDIR="{install_dir}"\' -Wait\n'
        f'Start-Process -FilePath "{layout["pythonw"]}" -ArgumentList \'"{layout["launcher"]}"\'\n'
    )


def start_installer(setup: Path, layout: dict, pid: int | None = None) -> Path:
    """Run the updater script detached: it waits for this process, installs, restarts the app."""
    script = setup.parent / "update.ps1"
    script.write_text(updater_script(setup, layout, pid or os.getpid()), encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(  # noqa: S603
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info("update: installer %s scheduled after exit of pid %s", setup, pid or os.getpid())
    return script


def install_latest(result: dict) -> dict:
    layout = installed_layout()
    if layout is None:
        raise UpdateError("not_supported")
    if not result.get("newer") or not result.get("url"):
        raise UpdateError("no newer version")
    dest = DEFAULT_DIR / "updates" / Path(str(result["url"])).name
    download(result["url"], result.get("digest"), dest)
    script = start_installer(dest, layout)
    return {"setup": str(dest), "script": str(script), "version": result.get("latest")}
