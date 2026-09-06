"""FastAPI application: REST control + WebSocket state feed + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import MISSING
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from semishigure import __version__
from semishigure.core.controller import ScheduleStep, parse_schedule, preset_schedule
from semishigure.core.run import ABSOLUTE_MAX_CONCURRENCY, ProdConfirmationRequired, Run
from semishigure.core.store import RunStore
from semishigure.pbx.adapter import make_adapter
from semishigure.pbx.profile import PbxProfile, ProfileStore, build_executor
from semishigure.pbx.provision import Provisioner, ProvisionError, ProvisionPlan, ProvisionStore, flavor_of
from semishigure.plugins.registry import describe_builtin
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
    schedule: str | None = None  # "5:180,10:180,20:180" applied right after start
    preset: str | None = None  # scenario preset name applied right after start


class PrecheckRequest(BaseModel):
    scenario: str
    pbx_profile: str | None = None
    monitor: bool = True
    hold_seconds: float = 6.0
    ignore_register_failure: bool = True


class ScenarioCreate(BaseModel):
    name: str
    template: str | None = None  # copy from this scenario file


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


class SecretBody(BaseModel):
    value: str


class SecretCheck(BaseModel):
    names: list[str]


class ProvisionRequest(BaseModel):
    profile: str
    caller: str = "9100"
    answerers: list[str] = ["9001", "9002", "9003", "9004"]
    ring_group: str = "8001"
    max_calls: int = 5
    group_limit: int = 20
    secret_prefix: str = "ext"
    domain: str = ""
    context: str = "default"
    directory: str = "default"
    db_password_ref: str = ""


class GuideScenario(BaseModel):
    """Inputs of the setup guide (docs: README「セットアップガイド」); the server writes the YAML."""

    name: str
    pbx_profile: str | None = None
    host: str = "127.0.0.1"
    sip_port: int = 5060
    domain: str = ""
    environment: str = "dev"
    caller_user: str
    caller_secret: str
    destination: str
    answerers: list[dict[str, Any]]  # [{user, secret, max_calls}]
    caller_port: int = 5070
    answerer_port: int = 5080
    rtp_port_start: int = 20000
    rtp_port_end: int = 20999
    call_duration: float = 60
    audio: str = "synth:60"
    plugins: bool = True
    monitor: bool = True
    pbx_type: str = "freeswitch"
    overwrite: bool = False


class AppState:
    def __init__(self, scenario_dir: Path, store: RunStore | None):
        self.scenario_dir = scenario_dir
        self.store = store
        self.secrets = SecretStore()
        self.profiles = ProfileStore()
        self.provision = ProvisionStore()
        self.run: Run | None = None
        self.precheck_run: Run | None = None
        self.precheck: dict | None = None  # {"running", "started_at", "hold_seconds", "scenario", "result"}
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
        base = {"version": __version__, "scenario_dir": str(self.scenario_dir), "run": None, "now": time.time(), "precheck": None}
        if self.run is not None:
            base["run"] = self.run.snapshot()
        if self.precheck is not None:
            pc = dict(self.precheck)
            if pc.get("running"):
                pc["elapsed_s"] = round(time.time() - pc["started_at"], 1)
            base["precheck"] = pc
        return base


def create_app(scenario_dir: Path | str = "examples", store: RunStore | None = None) -> FastAPI:
    state = AppState(Path(scenario_dir), store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # server shutdown (SIGTERM / Ctrl-C): hang up every call, restore plugins, unregister
        for r in (state.run, state.precheck_run):
            if r is not None and not r.finished:
                log.warning("server shutting down with a run in progress: stopping it")
                try:
                    await asyncio.wait_for(r.stop(), 30)
                except Exception as exc:  # noqa: BLE001
                    log.error("run stop on shutdown failed: %s", exc)

    app = FastAPI(title="Semishigure", version=__version__, lifespan=lifespan)
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
                out.append({"file": p.name, "name": sc.name, "description": sc.description, "pbx_profile": sc.pbx_profile, "plugins": [k for k, v in sc.plugins.items() if not (isinstance(v, dict) and v.get("enabled") is False)], "pbx": {"host": sc.pbx.host, "domain": sc.pbx.domain, "environment": sc.pbx.environment}, "presets": list(sc.load.presets), "target": sc.load.target_concurrency, "call_duration": sc.load.call_duration, "ramp_rate": sc.load.ramp_rate})
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

    @app.get("/api/plugins")
    async def list_plugins() -> list[dict]:
        return describe_builtin()

    # -- PBX profiles ------------------------------------------------------------

    @app.get("/api/pbx/profiles")
    async def list_profiles() -> dict:
        defaults = {f.name: (f.default if f.default is not MISSING else (f.default_factory() if f.default_factory is not MISSING else None)) for f in dc_fields(PbxProfile)}
        return {"path": str(state.profiles.path), "profiles": [p.public() for p in state.profiles.load_all()], "fields": list(PbxProfile.__dataclass_fields__), "defaults": defaults}

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

    # -- secrets (names only ever leave the server; values go into the encrypted store) ----

    @app.get("/api/secrets")
    async def list_secrets() -> dict:
        import os

        env = sorted(k[len("SEMISHIGURE_SECRET_") :].lower() for k in os.environ if k.startswith("SEMISHIGURE_SECRET_"))
        try:
            names = state.secrets.names()
        except SecretError as exc:
            return {"store": [], "env": env, "error": str(exc), "file": str(state.secrets.file)}
        return {"store": names, "env": env, "file": str(state.secrets.file)}

    @app.post("/api/secrets/check")
    async def check_secrets(body: SecretCheck) -> dict:
        out = {}
        for n in body.names:
            try:
                state.secrets.resolve(f"secret:{n}")
                out[n] = True
            except SecretError:
                out[n] = False
        return {"resolved": out, "ok": all(out.values())}

    @app.post("/api/secrets/{name}")
    async def set_secret(name: str, body: SecretBody) -> dict:
        if not name or not all(ch.isalnum() or ch in "_-." for ch in name):
            raise HTTPException(400, "bad secret name (letters, digits, _ - . only)")
        if not body.value:
            raise HTTPException(400, "empty value")
        try:
            state.secrets.set(name, body.value)
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"stored": name, "file": str(state.secrets.file)}

    # -- PBX provisioning (extensions + ring group created on the PBX) ------------------

    @app.get("/api/provision")
    async def list_provision() -> dict:
        return {"provisioned": state.provision.load_all(), "path": str(state.provision.path)}

    @app.post("/api/provision")
    async def provision(req: ProvisionRequest) -> dict:
        profile = state.profiles.get(req.profile)
        if profile is None:
            raise HTTPException(404, f"PBX profile {req.profile!r} not found")
        if state.provision.get(req.profile):
            raise HTTPException(409, f"{req.profile} は既にプロビジョニング済みです。先に元に戻してください")
        plan = ProvisionPlan(caller=req.caller.strip(), answerers=[a.strip() for a in req.answerers if a.strip()], ring_group=req.ring_group.strip(), max_calls=req.max_calls, group_limit=req.group_limit, secret_prefix=req.secret_prefix.strip() or "ext", domain=req.domain.strip() or profile.domain, context=req.context.strip() or "default", directory=req.directory.strip() or "default", db_password_ref=req.db_password_ref.strip())
        if not plan.caller or not plan.answerers or not plan.ring_group:
            raise HTTPException(400, "caller, answerers and ring_group are required")
        executor = build_executor(profile, state.secrets)
        adapter = None
        try:
            await executor.connect()
            try:
                adapter = make_adapter(profile, executor, state.secrets)
                await adapter.connect()
            except Exception as exc:  # noqa: BLE001
                log.info("provision: adapter unavailable, using the CLI (%s)", exc)
                adapter = None
            record = await Provisioner(profile, executor, state.secrets, adapter).apply(plan)
        except ProvisionError as exc:
            raise HTTPException(400, str(exc)) from exc
        except SecretError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            for closer in (adapter, executor):
                if closer is not None:
                    try:
                        await closer.close()
                    except Exception:  # noqa: BLE001
                        pass
        state.provision.put(req.profile, record)
        return {"record": record, "flavor": flavor_of(profile)}

    @app.delete("/api/provision/{name}")
    async def unprovision(name: str) -> dict:
        record = state.provision.get(name)
        if record is None:
            raise HTTPException(404, "not provisioned")
        profile = state.profiles.get(name)
        if profile is None:
            raise HTTPException(404, f"PBX profile {name!r} not found")
        executor = build_executor(profile, state.secrets)
        try:
            await executor.connect()
            res = await Provisioner(profile, executor, state.secrets, None).remove(record)
        except ProvisionError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            try:
                await executor.close()
            except Exception:  # noqa: BLE001
                pass
        state.provision.delete(name)
        return res

    @app.post("/api/guide/scenario")
    async def guide_scenario(req: GuideScenario) -> dict:
        name = req.name.strip()
        if not name or "/" in name or "\\" in name:
            raise HTTPException(400, "bad name")
        p = state.scenario_dir / (name if name.endswith((".yaml", ".yml")) else f"{name}.yaml")
        if p.exists() and not req.overwrite:
            raise HTTPException(409, f"{p.name} exists")
        exts = [{"user": str(a["user"]), "password_ref": f"secret:{a['secret']}", "max_calls": int(a.get("max_calls", 5))} for a in req.answerers if a.get("user")]
        if not exts:
            raise HTTPException(400, "at least one answerer extension is required")
        data: dict[str, Any] = {
            "name": name,
            "description": f"setup guide: {req.caller_user} -> {req.destination} -> {', '.join(e['user'] for e in exts)}",
            "pbx": {"host": req.host, "sip_port": req.sip_port, "domain": req.domain, "caller_port": req.caller_port, "answerer_port": req.answerer_port, "rtp_port_start": req.rtp_port_start, "rtp_port_end": req.rtp_port_end, "environment": req.environment},
            "caller": {"auth_user": str(req.caller_user), "auth_password_ref": f"secret:{req.caller_secret}", "from_number": str(req.caller_user), "destination": str(req.destination), "audio": req.audio},
            "answerer": {"extensions": exts, "audio": req.audio},
            "load": {"target_concurrency": 1, "ramp_rate": "1/s", "call_duration": f"{int(req.call_duration)}s", "presets": {"A": {"steps": [5, 10, 20]}, "B": {"steps": [10, 20, 30]}}},
        }
        if req.pbx_profile:
            data["pbx_profile"] = req.pbx_profile
        if req.monitor:
            data["monitor"] = {"interval": "2s", "commands": [{"name": "channels_count", "api": "show channels count"} if req.pbx_type == "freeswitch" else {"name": "channels_count", "api": "core show channels count", "parse": "number"}]}
        if req.plugins:
            if req.pbx_type == "asterisk":
                data["plugins"] = {"log_patterns": {"counters": [{"name": "warnings", "regex": "WARNING\\["}, {"name": "errors", "regex": "ERROR\\["}]}}
            else:
                data["plugins"] = {"log_patterns": {"key": "^([0-9a-f-]{36})", "counters": [{"name": "warnings", "regex": "\\[WARNING\\]"}], "timers": [{"name": "channel_lifetime", "start": f"New Channel sofia/internal/{req.caller_user}", "end": f"Close Channel sofia/internal/{req.caller_user}"}]}}
        try:
            scenario_from_dict(data, base_dir=state.scenario_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid scenario: {exc}") from exc
        head = "# Semishigure のセットアップガイドが作成したシナリオ。パスワードは secret: 参照（値は暗号化ストアか環境変数）。\n"
        text = head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        p.write_text(text, encoding="utf-8")
        return {"file": p.name, "yaml": text}

    # -- run control ------------------------------------------------------------

    @app.post("/api/run/start")
    async def start_run(req: StartRequest) -> dict:
        async with state.lock:
            if state.run is not None and not state.run.finished:
                raise HTTPException(409, "a run is already in progress")
            if state.precheck is not None and state.precheck.get("running"):
                raise HTTPException(409, "a precheck is in progress")
            sc = load_scenario(state.scenario_path(req.scenario))
            profile = None
            if req.pbx_profile:
                profile = state.profiles.get(req.pbx_profile)
                if profile is None:
                    raise HTTPException(404, f"PBX profile {req.pbx_profile!r} not found")
            try:
                run = Run(sc, name=req.name, secrets=state.secrets, store=state.store, target=req.target, ramp_rate=req.ramp_rate, call_duration=req.call_duration, max_concurrency=req.max_concurrency, max_total_calls=req.max_total_calls, confirm_prod=req.confirm_prod, on_event=lambda kind, ev: None, profile=profile, profile_store=state.profiles, monitor_enabled=req.monitor, install_signal_handlers=False)
            except ProdConfirmationRequired as exc:
                raise HTTPException(428, str(exc)) from exc
            try:
                await run.start(ignore_register_failure=req.ignore_register_failure)
            except SecretError as exc:
                raise HTTPException(400, str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(502, str(exc)) from exc
            previous = state.run
            state.run = run
            try:
                if req.preset:
                    preset = sc.load.presets.get(req.preset)
                    if preset is None:
                        raise HTTPException(404, f"preset {req.preset!r} not found")
                    steps, _, _ = preset_schedule(preset, run.config.call_duration)
                    run.controller.run_schedule(steps, then_target=0)
                elif req.schedule:
                    run.controller.run_schedule(parse_schedule(req.schedule, run.config.call_duration), then_target=0)
            except (HTTPException, ValueError) as exc:
                await run.stop()
                state.run = previous
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(400, f"invalid schedule: {exc}") from exc
            return run.snapshot(series_tail=0, calls_tail=0)

    @app.post("/api/precheck")
    async def run_precheck(req: PrecheckRequest) -> dict:
        from semishigure.core.precheck import precheck

        async with state.lock:
            if state.run is not None and not state.run.finished:
                raise HTTPException(409, "a run is in progress")
            if state.precheck is not None and state.precheck.get("running"):
                raise HTTPException(409, "a precheck is in progress")
            sc = load_scenario(state.scenario_path(req.scenario))
            profile = state.profiles.get(req.pbx_profile) if req.pbx_profile else None
            if req.pbx_profile and profile is None:
                raise HTTPException(404, f"PBX profile {req.pbx_profile!r} not found")
            try:
                run = Run(sc, name=f"precheck-{sc.name}", secrets=state.secrets, store=None, target=0, confirm_prod=True, profile=profile, profile_store=state.profiles, monitor_enabled=req.monitor, install_signal_handlers=False)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, str(exc)) from exc
            # the precheck run is kept apart from state.run so the UI stays on the form (docs/ui-ux-proposal.md F1)
            state.precheck_run = run
            state.precheck = {"running": True, "started_at": time.time(), "hold_seconds": req.hold_seconds, "scenario": sc.name, "pbx_profile": req.pbx_profile, "result": None}
            try:
                result = await precheck(run, hold_seconds=req.hold_seconds, ignore_register_failure=req.ignore_register_failure)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "items": [{"name": "起動", "ok": False, "detail": str(exc)}], "elapsed_s": 0}
            finally:
                state.precheck_run = None
                state.precheck = {"running": False, "finished_at": time.time(), "scenario": sc.name, "pbx_profile": req.pbx_profile, "result": None}
            state.precheck["result"] = result
            return result

    @app.post("/api/scenarios")
    async def create_scenario(req: ScenarioCreate) -> dict:
        name = req.name.strip()
        if not name or "/" in name or "\\" in name:
            raise HTTPException(400, "bad name")
        p = state.scenario_dir / (name if name.endswith((".yaml", ".yml")) else f"{name}.yaml")
        if p.exists():
            raise HTTPException(409, f"{p.name} exists")
        if req.template:
            text = state.scenario_path(req.template).read_text(encoding="utf-8")
        else:
            text = (Path(__file__).resolve().parent.parent.parent / "examples" / "dev-freeswitch.yaml").read_text(encoding="utf-8") if (Path(__file__).resolve().parent.parent.parent / "examples" / "dev-freeswitch.yaml").exists() else "name: new-scenario\n"
        p.write_text(text, encoding="utf-8")
        return {"file": p.name}

    @app.delete("/api/scenarios/{name}")
    async def delete_scenario(name: str) -> dict:
        p = state.scenario_path(name)
        p.unlink()
        return {"deleted": p.name}

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

    @app.get("/api/runs/export.xlsx")
    async def export_runs(ids: str = "") -> Response:
        from io import BytesIO

        from semishigure.report.xlsx import build_workbook

        if state.store is None:
            raise HTTPException(404, "no store")
        run_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()] or [r["id"] for r in state.store.list_runs(limit=10)][::-1]
        runs = [r for r in (state.store.get_run(i) for i in run_ids) if r is not None]
        if not runs:
            raise HTTPException(404, "no runs")
        buf = BytesIO()
        build_workbook(runs).save(buf)
        name = "semishigure-report-" + "-".join(str(r["id"]) for r in runs) + ".xlsx"
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int, max_points: int = 1500) -> dict:
        if state.store is None:
            raise HTTPException(404, "no store")
        r = state.store.get_run(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        if max_points > 0 and r.get("samples"):
            r = dict(r)
            r["samples_total"] = len(r["samples"])
            r["samples"] = decimate_samples(r["samples"], max_points)
        return r

    @app.get("/api/runs/{run_id}/rows")
    async def get_run_rows(run_id: int) -> dict:
        """The record-sheet rows (same labels and order as the xlsx) for one run."""
        from semishigure.report.xlsx import generic_rows

        if state.store is None:
            raise HTTPException(404, "no store")
        r = state.store.get_run(run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        rows = []
        section = ""
        for label, value in generic_rows(r):
            if value is None and str(label).startswith("§"):
                section = str(label)[1:]
                rows.append({"section": section, "label": None, "value": None})
            else:
                rows.append({"section": section, "label": label, "value": value})
        return {"id": run_id, "name": r.get("name"), "rows": rows}

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


def decimate_samples(samples: list[dict], max_points: int) -> list[dict]:
    """Reduce a sample list to about max_points per kind, keeping the max of every
    numeric field inside each bucket (peaks such as rtp_late_max_ms survive)."""
    by_kind: dict[str | None, list[dict]] = {}
    for p in samples:
        by_kind.setdefault(p.get("kind"), []).append(p)
    out: list[dict] = []
    for pts in by_kind.values():
        if len(pts) <= max_points:
            out.extend(pts)
            continue
        step = len(pts) / max_points
        i = 0.0
        while i < len(pts):
            bucket = pts[int(i) : int(i + step)] or [pts[int(i)]]
            merged = dict(bucket[-1])
            for k in merged:
                vals = [b.get(k) for b in bucket if isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool)]
                if vals and k != "t":
                    merged[k] = max(vals)
            out.append(merged)
            i += step
    out.sort(key=lambda p: p.get("t", 0))
    return out
