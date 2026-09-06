"""Update check / self-update helpers and the debug log endpoint (no network)."""

import hashlib
import io
import logging
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from semishigure import __version__
from semishigure import update as up
from semishigure.api.app import create_app


def _release(tag: str, digest: str = "sha256:abc", url: str | None = None) -> dict:
    return {"tag_name": tag, "html_url": f"https://github.com/{up.REPO}/releases/tag/{tag}", "body": "notes", "assets": [{"name": f"Semishigure-{tag.lstrip('v')}-setup.exe", "browser_download_url": url or f"{up.DOWNLOAD_PREFIX}{tag}/Semishigure-{tag.lstrip('v')}-setup.exe", "digest": digest, "size": 123}]}


def test_parse_version():
    assert up.parse_version("v1.10.2") == (1, 10, 2) and up.parse_version("1.0.5") == (1, 0, 5)
    assert up.parse_version("v2.0.0-rc1") == (2, 0, 0)
    assert up.parse_version("v1.2.10") > up.parse_version("v1.2.3")


def test_check_newer_older_and_error(monkeypatch):
    up._cache.clear()
    r = up.check(force=True, fetch=lambda: _release("v99.0.0"))
    assert r["newer"] and r["latest"] == "99.0.0" and r["url"].endswith("-setup.exe") and r["error"] is None
    r = up.check(force=True, fetch=lambda: _release("v" + __version__))
    assert not r["newer"]
    r = up.check(force=True, fetch=lambda: (_ for _ in ()).throw(OSError("offline")))
    assert r["error"] == "offline" and not r["newer"] and r["current"] == __version__
    # cached: a later call without force returns the same (error) result until the short error TTL expires
    assert up.check(fetch=lambda: _release("v99.0.0"))["error"] == "offline"
    up._cache.clear()


def test_download_verifies_checksum(tmp_path: Path):
    body = b"installer bytes" * 1000
    good = "sha256:" + hashlib.sha256(body).hexdigest()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    opener = lambda req, timeout: Resp(body)  # noqa: E731
    url = f"{up.DOWNLOAD_PREFIX}v9/Semishigure-9-setup.exe"
    out = up.download(url, good, tmp_path / "s.exe", opener=opener)
    assert out.read_bytes() == body
    with pytest.raises(up.UpdateError):
        up.download(url, "sha256:" + "0" * 64, tmp_path / "bad.exe", opener=opener)
    assert not (tmp_path / "bad.exe").exists()
    with pytest.raises(up.UpdateError):
        up.download("https://evil.example/x.exe", good, tmp_path / "x.exe", opener=opener)
    with pytest.raises(up.UpdateError):
        up.download(url, None, tmp_path / "y.exe", opener=opener)


def test_installer_params_follow_the_install_dir(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert up.installer_params(Path(r"C:\Users\me\AppData\Local\Programs\Semishigure")) == '/S /currentuser /INSTDIR="C:\\Users\\me\\AppData\\Local\\Programs\\Semishigure"'
    assert up.installer_params(Path(r"C:\Program Files\Semishigure")).startswith("/S /allusers ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake installer")
def test_apply_update_waits_installs_and_restarts(tmp_path: Path):
    import subprocess

    # a fake installer that records its arguments, and a fake app to restart
    setup = tmp_path / "setup.sh"
    setup.write_text("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/installed.txt\"\nexit 0\n")
    setup.chmod(0o755)
    launcher = tmp_path / "launch.py"
    launcher.write_text("open(__file__ + '.started', 'w').write('x')\n")
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    log = tmp_path / "update.log"
    rc = up.apply_update(setup, victim.pid, tmp_path, launcher, Path(sys.executable), log, wait_timeout=30)
    assert rc == 0
    text = log.read_text(encoding="utf-8")
    assert "waiting for pid" in text and "installer exit code 0" in text and "done" in text
    assert "/S" in (tmp_path / "installed.txt").read_text() and "/INSTDIR=" in (tmp_path / "installed.txt").read_text()
    for _ in range(50):
        if (tmp_path / "launch.py.started").exists():
            break
        time.sleep(0.1)
    assert (tmp_path / "launch.py.started").exists()
    victim.wait()


def test_debug_log_and_update_endpoints(tmp_path: Path, monkeypatch):
    app = create_app(scenario_dir=tmp_path / "scen", store=None)
    c = TestClient(app)
    logging.getLogger("semishigure.test").info("hello from the test %s", 42)
    r = c.get("/api/debug/log?lines=50").json()
    assert any("hello from the test 42" in line for line in r["lines"])
    assert [k for k, _ in r["env"]][:2] == ["Ver.", "OS"] and r["env"][0][1] == __version__
    assert c.post("/api/debug/client", json={"message": "TypeError: x is undefined"}).json()["ok"]
    assert any("TypeError: x is undefined" in line for line in c.get("/api/debug/log").json()["lines"])
    up._cache.clear()
    monkeypatch.setattr(up, "fetch_latest", lambda: _release("v99.0.0"))
    r = c.get("/api/update/check?force=true").json()
    assert r["newer"] and r["latest"] == "99.0.0" and r["can_install"] is False
    # not the desktop build: install is refused, nothing is downloaded
    assert c.post("/api/update/install").status_code == 400
    up._cache.clear()
