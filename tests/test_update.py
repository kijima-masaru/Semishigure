"""Update check / self-update helpers and the debug log endpoint (no network)."""

import hashlib
import io
import logging
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


def test_updater_script_targets_the_same_install_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    layout = {"install_dir": Path(r"C:\Users\me\AppData\Local\Programs\Semishigure"), "launcher": Path(r"C:\Users\me\AppData\Local\Programs\Semishigure\Semishigure.launch.pyw"), "pythonw": Path(r"C:\Users\me\AppData\Local\Programs\Semishigure\Python\pythonw.exe")}
    s = up.updater_script(tmp_path / "Semishigure-9-setup.exe", layout, 4242)
    assert "Get-Process -Id $target" in s and "$target = 4242" in s
    assert '/S /currentuser /INSTDIR="C:\\Users\\me\\AppData\\Local\\Programs\\Semishigure"' in s
    assert "pythonw.exe" in s and "Semishigure.launch.pyw" in s
    layout["install_dir"] = Path(r"C:\Program Files\Semishigure")
    assert "/allusers" in up.updater_script(tmp_path / "x.exe", layout, 1)


def test_debug_log_and_update_endpoints(tmp_path: Path, monkeypatch):
    app = create_app(scenario_dir=tmp_path / "scen", store=None)
    c = TestClient(app)
    logging.getLogger("semishigure.test").info("hello from the test %s", 42)
    r = c.get("/api/debug/log?lines=50").json()
    assert any("hello from the test 42" in line for line in r["lines"])
    assert [k for k, _ in r["env"]][:2] == ["版", "OS"] and r["env"][0][1] == __version__
    assert c.post("/api/debug/client", json={"message": "TypeError: x is undefined"}).json()["ok"]
    assert any("TypeError: x is undefined" in line for line in c.get("/api/debug/log").json()["lines"])
    up._cache.clear()
    monkeypatch.setattr(up, "fetch_latest", lambda: _release("v99.0.0"))
    r = c.get("/api/update/check?force=true").json()
    assert r["newer"] and r["latest"] == "99.0.0" and r["can_install"] is False
    # not the desktop build: install is refused, nothing is downloaded
    assert c.post("/api/update/install").status_code == 400
    up._cache.clear()
