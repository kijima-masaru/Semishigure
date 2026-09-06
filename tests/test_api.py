"""API round trips with the FastAPI test client (loopback scenario, no PBX)."""

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from semishigure.api.app import create_app
from semishigure.core.store import RunStore
from semishigure.pbx.profile import ProfileStore


def _scenario_yaml() -> str:
    return yaml.safe_dump(
        {
            "name": "loop-api",
            "pbx": {"host": "127.0.0.1", "sip_port": 5281, "domain": "loop.test", "local_ip": "127.0.0.1", "caller_port": 5271, "answerer_port": 5281, "rtp_port_start": 25000, "rtp_port_end": 25199},
            "caller": {"auth_user": "9100", "auth_password_ref": "literal:x", "destination": "9001", "audio": "synth:3"},
            "answerer": {"extensions": [{"user": "9001", "password_ref": "literal:x", "max_calls": 5}], "audio": "synth:3"},
            "load": {"target_concurrency": 1, "ramp_rate": "5/s", "call_duration": "30s"},
        }
    )


def test_scenarios_profiles_precheck_and_export(tmp_path: Path, monkeypatch):
    scen_dir = tmp_path / "scenarios"
    scen_dir.mkdir()
    (scen_dir / "loop.yaml").write_text(_scenario_yaml())
    monkeypatch.setenv("SEMISHIGURE_HOME", str(tmp_path / "home"))
    app = create_app(scenario_dir=scen_dir, store=RunStore(tmp_path / "runs.sqlite3"))
    app.state.semishigure.profiles = ProfileStore(tmp_path / "profiles.yaml")
    with TestClient(app) as c:
        # scenarios: list, read, create from template, save (validated), delete
        assert [s["file"] for s in c.get("/api/scenarios").json()] == ["loop.yaml"]
        assert c.get("/api/scenarios/loop").json()["parsed"]["name"] == "loop-api"
        assert c.post("/api/scenarios", json={"name": "copy", "template": "loop.yaml"}).json()["file"] == "copy.yaml"
        assert c.put("/api/scenarios/copy", json={"yaml": "name: x\ncaller: [1"}).status_code == 400
        assert c.put("/api/scenarios/copy", json={"yaml": "name: renamed\n"}).status_code == 200
        assert c.get("/api/scenarios/copy").json()["parsed"]["name"] == "renamed"
        assert c.delete("/api/scenarios/copy").json()["deleted"] == "copy.yaml"
        # profiles: field list, save with reference-only secrets, reject plain passwords
        fields = c.get("/api/pbx/profiles").json()["fields"]
        assert "sip_transport" in fields and "esl_password_ref" in fields
        assert c.put("/api/pbx/profiles/p1", json={"profile": {"host": "10.0.0.1", "esl_password_ref": "ClueCon"}}).status_code == 400
        assert c.put("/api/pbx/profiles/p1", json={"profile": {"host": "10.0.0.1", "esl_password_ref": "secret:esl", "extra": {"k": 1}}}).status_code == 200
        assert c.get("/api/pbx/profiles").json()["profiles"][0]["name"] == "p1"
        assert c.get("/api/plugins").json()[0]["name"] == "log_patterns"
        # precheck against the in-process answerer (REGISTER gets 405 -> ignored)
        r = c.post("/api/precheck", json={"scenario": "loop.yaml", "monitor": False, "hold_seconds": 1.0})
        assert r.status_code == 200, r.text
        body = r.json()
        names = {i["name"]: i for i in body["items"]}
        assert names["発信 → 応答"]["ok"] and names["RTP 送出（発信側）"]["ok"] and names["RTP 受信（発信側）"]["ok"] and names["BYE"]["ok"]
        assert names["REGISTER 9001"]["ok"] is False  # no registrar on the loopback
        assert c.get("/api/state").json()["run"] is None
        # a short run through the API, then the xlsx export
        r = c.post("/api/run/start", json={"scenario": "loop.yaml", "target": 1, "ignore_register_failure": True})
        assert r.status_code == 200, r.text
        import time

        time.sleep(1.5)
        st = c.get("/api/state").json()["run"]
        assert st["controller"]["established"] == 1
        c.post("/api/run/target", json={"target": 0})
        time.sleep(0.8)
        summary = c.post("/api/run/stop").json()
        assert summary["calls_started"] == 1
        assert c.delete("/api/pbx/profiles/p1").json()["deleted"] is True
        x = c.get("/api/runs/export.xlsx")
        assert x.status_code == 200 and x.headers["content-type"].startswith("application/vnd.openxmlformats") and len(x.content) > 5000
        runs = c.get("/api/runs").json()
        assert runs and c.get(f"/api/runs/{runs[0]['id']}").json()["summary"]["calls_started"] == 1
