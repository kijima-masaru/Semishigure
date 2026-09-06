"""FastAPI application: REST control + WebSocket state feed + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from semishigure import __version__
from semishigure.core.controller import ScheduleStep, parse_schedule, preset_schedule
from semishigure.core.run import ABSOLUTE_MAX_CONCURRENCY, ProdConfirmationRequired, Run
from semishigure.core.store import RunStore
from semishigure.pbx.adapter import make_adapter
from semishigure.pbx.profile import PbxProfile, ProfileStore, build_executor
from semishigure.scenario.model import load_scenario, scenario_from_dict
from semishigure.secrets import SecretError, SecretStore

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"


class StartRequest(BaseModel):
    scenario: str
    pbx_profile: str | None = None
    monitor: bool = True
    target: int | None = None
    ramp_rate: float | None = None
    call_duration: float | None = None
    max_concurrency: int = ABSOLUTE_MAX_CONCURRENCY
    max_total_calls: int | None = None
    confirm_prod: bool = False
    name: str = ""
    ignore_register_failure: bool = False


class TargetRequest(BaseModel):
    target: int


class DeltaRequest(BaseModel):
    delta: int


class RateRequest(BaseModel):
    ramp_rate: float | None = None
    call_duration: float | None = None
    burst: bool | None = None


class ScheduleRequest(BaseModel):
    steps: list[dict[str, Any]] | None = None  # [{target, seconds}]
    text: str | None = None  # "5:60,20:60,8:60"
    preset: str | None = None
    then_target: int | None = 0


class ScenarioSave(BaseModel):
    yaml: str


class ProfileBody(BaseModel):
    profile: dict[str, Any]


class AppState:
    def __init__(self, scenario_dir: Path, store: RunStore | None):
        self.scenario_dir = scenario_dir
        self.store = store
        self.secrets = SecretStore()
        self.profiles = ProfileStore()
        self.run: Run | None = None
        self.lock = asyncio.Lock()
        self.clients: set[WebSocket] = set()

    def scenario_files(self) -> list[Path]:
        return sorted(p for p in self.scenario_dir.glob("*.y*ml") if p.is_file())

    def scenario_path(self, name: str) -> Path:
        for p in self.scenario_files():
            if p.name == name or p.stem == name:
                return p
        raise HTTPException(404, f"scenario {name!r} not found")

    def require_run(self) -> Run:
        if self.run is None or self.run.finished:
            raise HTTPException(409, "no run in progress")
        return self.run

    def snapshot(self) -> dict:
        base = {"version": __version__, "scenario_dir": str(self.scenario_dir), "run": None}
        if self.run is not None:
            base["run"] = self.run.snapshot()
        return base


def create_app(scenario_dir: Path | str = "examples", store: RunStore | None = None) -> FastAPI:
    state = AppState(Path(scenario_dir), store)
    app = FastAPI(title="Semishigure", version=__version__)
    app.state.semishigure = state
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def api_state() -> dict:
        return state.snapshot()

    # -- scenarios ------------------------------------------------------------

    @app.get("/api/scenarios")
    async def list_scenarios() -> list[dict]:
        out = []
        for p in state.scenario_files():
            try:
                sc = load_scenario(p)
                out.append({"file": p.name, "name": sc.name, "description": sc.description, "pbx_profile": sc.pbx_profile, "pbx": {"host": sc.pbx.host, "domain": sc.pbx.domain, "environment": sc.pbx.environment}, "presets": list(sc.load.presets), "target": sc.load.target_concurrency, "call_duration": sc.load.call_duration, "ramp_rate": sc.load.ramp_rate})
            except Exception as exc:  # noqa: BLE001
                out.append({"file": p.name, "error": str(exc)})
        return out

    @app.get("/api/scenarios/{name}")
    async def get_scenario(name: str) -> dict:
        p = state.scenario_path(name)
        text = p.read_text(encoding="utf-8")
        return {"file": p.name, "yaml": text, "parsed": yaml.safe_load(text)}

    @app.put("/api/scenarios/{name}")
    async def save_scenario(name: str, body: ScenarioSave) -> dict:
        if "/" in name or "\\" in name:
            raise HTTPException(400, "bad name")
        p = state.scenario_dir / (name if name.endswith((".yaml", ".yml")) else f"{name}.yaml")
        try:
            data = yaml.safe_load(body.yaml) or {}
            scenario_from_dict(data, base_dir=state.scenario_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid scenario: {exc}") from exc
        p.write_text(body.yaml, encoding="utf-8")
        return {"file": p.name}

    # -- PBX profiles ------------------------------------------------------------

    @app.get("/api/pbx/profiles")
    async def list_profiles() -> dict:
        return {"path": str(state.profiles.path), "profiles": [p.public() for p in state.profiles.load_all()], "fields": list(PbxProfile.__dataclass_fields__)}

    @app.put("/api/pbx/profiles/{name}")
    async def save_profile(name: str, body: ProfileBody) -> dict:
        data = dict(body.profile)
        data["name"] = name
        for k, v in list(data.items()):
            if "password" in k or "passphrase" in k:
                if v and not str(v).startswith(("secret:", "env:", "literal:")):
                    raise HTTPException(400, f"{k} must be a reference (secret:NAME), not a value")
        try:
            profile = PbxProfile.from_dict(data)
        except TypeError as exc:
            raise HTTPException(400, str(exc)) from exc
        state.profiles.upsert(profile)
        return profile.public()

    @app.delete("/api/pbx/profiles/{name}")
    async def delete_profile(name: str) -> dict:
        return {"deleted": state.profiles.delete(name)}

    @app.post("/api/pbx/profiles/{name}/test")
    async def test_profile(name: str) -> dict:
        profile = state.profiles.get(name)
        if profile is None:
            raise HTTPException(404, "profile not found")
        adapter = make_adapter(profile, build_executor(profile, state.secrets), state.secrets)
        t0 = asyncio.get_running_loop().time()
        try:
            await adapter.connect()
            st = await adapter.status()
            pm = await adapter.process_metrics()
            regs = await adapter.registrations() if hasattr(adapter, "registrations") else []
            tail = await adapter.log_tail(5)
            return {
                "ok": True,
                "connect_ms": round((asyncio.get_running_loop().time() - t0) * 1000, 1),
                "adapter": adapter.describe(),
                "status": {"version": st.version, "uptime": st.uptime, "sessions": st.sessions, "sessions_peak": st.sessions_peak, "sessions_max": st.sessions_max, "sps_max": st.sps_max},
                "channels": await adapter.channels_count(),
                "registrations": regs,
                "process": {"pid": pm.pid, "cpu": pm.cpu_percent, "mem": pm.mem_percent, "threads": pm.threads, "rss_kb": pm.rss_kb, "top_threads": pm.top_threads[:5]},
                "log_tail": tail,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001
                pass

    # -- run control ------------------------------------------------------------

    @app.post("/api/run/start")
    async def start_run(req: StartRequest) -> dict:
        async with state.lock:
            if state.run is not None and not state.run.finished:
                raise HTTPException(409, "a run is already in progress")
            sc = load_scenario(state.scenario_path(req.scenario))
            profile = None
            if req.pbx_profile:
                profile = state.profiles.get(req.pbx_profile)
                if profile is None:
                    raise HTTPException(404, f"PBX profile {req.pbx_profile!r} not found")
            try:
                run = Run(sc, name=req.name, secrets=state.secrets, store=state.store, target=req.target, ramp_rate=req.ramp_rate, call_duration=req.call_duration, max_concurrency=req.max_concurrency, max_total_calls=req.max_total_calls, confirm_prod=req.confirm_prod, on_event=lambda kind, ev: None, profile=profile, profile_store=state.profiles, monitor_enabled=req.monitor)
            except ProdConfirmationRequired as exc:
                raise HTTPException(428, str(exc)) from exc
            try:
                await run.start(ignore_register_failure=req.ignore_register_failure)
            except SecretError as exc:
                raise HTTPException(400, str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(502, str(exc)) from exc
            state.run = run
            return run.snapshot(series_tail=0, calls_tail=0)

    @app.post("/api/run/stop")
    async def stop_run() -> dict:
        async with state.lock:
            run = state.require_run()
            await run.stop()
            return run.summary()

    @app.post("/api/run/target")
    async def set_target(req: TargetRequest) -> dict:
        run = state.require_run()
        run.controller.clear_schedule()
        return {"target": run.controller.set_target(req.target, "ui")}

    @app.post("/api/run/adjust")
    async def adjust(req: DeltaRequest) -> dict:
        run = state.require_run()
        run.controller.clear_schedule()
        return {"target": run.controller.adjust(req.delta, "ui")}

    @app.post("/api/run/rate")
    async def set_rate(req: RateRequest) -> dict:
        run = state.require_run()
        if req.ramp_rate is not None:
            run.controller.set_ramp_rate(req.ramp_rate)
        if req.call_duration is not None:
            run.controller.set_call_duration(req.call_duration)
        if req.burst is not None:
            run.controller.set_burst(req.burst)
        return run.controller.state()

    @app.post("/api/run/burst")
    async def burst(req: TargetRequest) -> dict:
        """One-shot burst: ramp is unlimited until the target is reached."""
        run = state.require_run()
        run.controller.clear_schedule()
        run.controller.set_burst(True)
        run.controller.set_target(req.target, "burst")

        async def _off() -> None:
            for _ in range(600):
                await asyncio.sleep(0.1)
                st = run.controller.state()
                if st["established"] + st["pending"] >= st["target"] or run.finished:
                    break
            run.controller.set_burst(False)

        asyncio.get_running_loop().create_task(_off())
        return run.controller.state()

    @app.post("/api/run/pause")
    async def pause() -> dict:
        run = state.require_run()
        run.controller.pause()
        return run.controller.state()

    @app.post("/api/run/resume")
    async def resume() -> dict:
        run = state.require_run()
        run.controller.resume()
        return run.controller.state()

    @app.post("/api/run/hangup_all")
    async def hangup_all() -> dict:
        run = state.require_run()
        await run.controller.hangup_all()
        return run.controller.state()

    @app.post("/api/run/schedule")
    async def schedule(req: ScheduleRequest) -> dict:
        run = state.require_run()
        steps: list[ScheduleStep]
        if req.preset:
            preset = run.scenario.load.presets.get(req.preset)
            if preset is None:
                raise HTTPException(404, f"preset {req.preset!r} not found")
            steps, _, _ = preset_schedule(preset, run.config.call_duration)
        elif req.text:
            steps = parse_schedule(req.text, run.config.call_duration)
        elif req.steps:
            steps = [ScheduleStep(int(s["target"]), float(s.get("seconds", run.config.call_duration))) for s in req.steps]
        else:
            raise HTTPException(400, "steps, text or preset required")
        run.controller.run_schedule(steps, then_target=req.then_target)
        return run.controller.state()

    @app.post("/api/run/schedule/clear")
    async def clear_schedule() -> dict:
        run = state.require_run()
        run.controller.clear_schedule()
        return run.controller.state()

    # -- past runs ---------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs() -> list[dict]:
        return state.store.list_runs() if state.store else []

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int) -> dict:
        if state.store is None:
            raise HTTPException(404, "no store")
        r = state.store.get_run(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return r

    # -- websocket feed -------------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        state.clients.add(websocket)
        try:
            while True:
                await websocket.send_text(json.dumps(state.snapshot(), ensure_ascii=False))
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
                except TimeoutError:
                    continue
                # optional commands over the socket (same as REST)
                try:
                    cmd = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if state.run is not None and not state.run.finished:
                    c = state.run.controller
                    if cmd.get("op") == "target":
                        c.clear_schedule()
                        c.set_target(int(cmd.get("value", c.target)), "ws")
                    elif cmd.get("op") == "adjust":
                        c.clear_schedule()
                        c.adjust(int(cmd.get("value", 0)), "ws")
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(websocket)

    @app.exception_handler(Exception)
    async def _err(_, exc: Exception) -> JSONResponse:
        log.exception("api error")
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app
