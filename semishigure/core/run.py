"""Run: scenario + engine + controller + stats + persistence for one load test."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from semishigure.core.call import CallRecord, CallState
from semishigure.core.controller import ControllerConfig, LoadController, ScheduleStep
from semishigure.core.engine import SipEngine
from semishigure.core.stats import RunStats
from semishigure.core.store import RunStore
from semishigure.pbx.adapter import PbxAdapter, make_adapter
from semishigure.pbx.monitor import Monitor, commands_from_scenario
from semishigure.pbx.profile import PbxProfile, ProfileStore, build_executor
from semishigure.plugins.base import PluginContext
from semishigure.plugins.manager import PluginManager
from semishigure.scenario.model import Scenario
from semishigure.secrets import SecretStore

log = logging.getLogger(__name__)

ABSOLUTE_MAX_CONCURRENCY = 50


class ProdConfirmationRequired(RuntimeError):
    pass


def apply_profile(scenario: Scenario, profile: PbxProfile) -> None:
    """The PBX profile is the source of truth for where the PBX is."""
    scenario.pbx.host = profile.host
    scenario.pbx.sip_port = profile.sip_port
    if profile.domain:
        scenario.pbx.domain = profile.domain
    scenario.pbx.environment = profile.environment


class Run:
    def __init__(
        self,
        scenario: Scenario,
        *,
        name: str = "",
        secrets: SecretStore | None = None,
        store: RunStore | None = None,
        target: int | None = None,
        ramp_rate: float | None = None,
        call_duration: float | None = None,
        max_concurrency: int = ABSOLUTE_MAX_CONCURRENCY,
        max_total_calls: int | None = None,
        confirm_prod: bool = False,
        record_rx_dir: Path | None = None,
        sip_trace: bool = False,
        on_event: Callable[[str, dict], None] | None = None,
        profile: PbxProfile | None = None,
        profile_store: ProfileStore | None = None,
        monitor_enabled: bool = True,
        install_signal_handlers: bool = True,
    ):
        self.scenario = scenario
        self.profile = profile or (profile_store or ProfileStore()).get(scenario.pbx_profile) if scenario.pbx_profile else profile
        if self.profile is not None:
            apply_profile(scenario, self.profile)
        self.monitor_enabled = monitor_enabled and self.profile is not None
        self.adapter: PbxAdapter | None = None
        self.monitor: Monitor | None = None
        self.monitor_error: str = ""
        self.plugins: PluginManager | None = None
        self.plugin_metrics: dict = {}
        self._plugin_task: asyncio.Task | None = None
        self.name = name or f"{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.store = store
        self.run_id: int | None = None
        self.stats = RunStats()
        self.on_event = on_event
        if scenario.pbx.environment.lower() == "prod" and not confirm_prod:
            raise ProdConfirmationRequired("PBX profile is tagged 'prod': explicit confirmation is required to start a run")
        cap = min(max_concurrency, ABSOLUTE_MAX_CONCURRENCY)
        if scenario.pbx.environment.lower() == "prod":
            cap = min(cap, 20)
        if self.profile is not None and self.profile.max_concurrency:
            cap = min(cap, self.profile.max_concurrency)
        self.config = ControllerConfig(
            target=target if target is not None else scenario.load.target_concurrency,
            ramp_rate=ramp_rate if ramp_rate is not None else scenario.load.ramp_rate,
            call_duration=call_duration if call_duration is not None else scenario.load.call_duration,
            max_concurrency=cap,
            max_total_calls=max_total_calls if max_total_calls is not None else scenario.load.max_total_calls,
        )
        self.engine = SipEngine(scenario, secrets or SecretStore(), record_rx_dir=record_rx_dir, sip_trace=sip_trace, on_event=self._on_call_event, install_signal_handlers=install_signal_handlers)
        self.engine.signal_callback = self.stop  # a signal stops the controller, plugins and SIP together
        self.controller = LoadController(self.engine, self.config, self.stats, on_change=self._on_controller_event)
        self._persist_task: asyncio.Task | None = None
        self._persisted_samples = 0
        self._persisted_events = 0
        self._persisted_calls = 0
        self._persisted_monitor = 0
        self.finished = False
        self.started_at: float | None = None

    # -- events ------------------------------------------------------------------------

    def _on_call_event(self, rec: CallRecord, event: str) -> None:
        if rec.role.value == "caller" and self.plugins is not None and len(self.plugins):
            if event == "established":
                self.plugins.call_established(rec)
            elif event.startswith("done:") and rec.t_established is not None:
                self.plugins.call_ended(rec)
        if self.on_event and event in ("established", "done:remote_bye") and rec.role.value == "caller":
            self.on_event("call", {"id": rec.id, "event": event})

    def _on_controller_event(self, kind: str, ev: dict) -> None:
        if self.on_event:
            self.on_event(kind, ev)

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self, ignore_register_failure: bool = False) -> None:
        self.started_at = time.time()
        if self.monitor_enabled and self.profile is not None:
            await self._start_monitor()
        await self._start_plugins()
        await self.engine.start()
        if not all(self.engine.registration_results.values()) and not ignore_register_failure:
            failed = [u for u, ok in self.engine.registration_results.items() if not ok]
            await self.engine.shutdown()
            raise RuntimeError(f"registration failed for {', '.join(failed)}")
        if self.plugins is not None and len(self.plugins):
            try:
                await self.plugins.pre_run()
            except Exception as exc:  # noqa: BLE001
                await self.plugins.post_run()
                await self.engine.shutdown()
                raise RuntimeError(f"plugin pre_run failed: {exc}") from exc
        if self.store is not None:
            self.run_id = self.store.create_run(self.name, self.scenario.name, yaml.safe_dump(self.scenario.raw, allow_unicode=True), {"host": self.scenario.pbx.host, "domain": self.scenario.pbx.domain, "environment": self.scenario.pbx.environment})
            self._persist_task = asyncio.get_running_loop().create_task(self._persist_loop())
        self.controller.start()

    async def _start_monitor(self) -> None:
        assert self.profile is not None
        try:
            executor = build_executor(self.profile, self.engine.secrets)
            self.adapter = make_adapter(self.profile, executor, self.engine.secrets)
            await self.adapter.connect()
            interval, commands = commands_from_scenario(self.scenario.monitor)
            self.monitor = Monitor(self.adapter, interval=interval, commands=commands)
            await self.monitor.start()
            self.stats.event("monitor", f"monitor started via {executor.kind} executor ({self.adapter.describe().get('esl')})")
        except Exception as exc:  # noqa: BLE001
            self.monitor_error = str(exc)
            self.stats.event("monitor", f"monitor unavailable: {exc}")
            log.warning("monitor unavailable: %s", exc)
            if self.adapter is not None:
                try:
                    await self.adapter.close()
                except Exception:  # noqa: BLE001
                    pass
            self.adapter = None
            self.monitor = None

    async def _start_plugins(self) -> None:
        ctx = PluginContext(run_name=self.name, scenario=self.scenario, adapter=self.adapter, secrets=self.engine.secrets, monitor=self.monitor)
        self.plugins = PluginManager(self.scenario.plugins, ctx)
        if not len(self.plugins):
            return
        self.engine.shutdown_hooks.append(self.plugins.post_run)
        if self.monitor is not None:
            self.monitor.plugins.append(self.plugins)
        else:
            self._plugin_task = asyncio.get_running_loop().create_task(self._plugin_metrics_loop())
        self.stats.event("plugins", "plugins loaded: " + ", ".join(p.name for p in self.plugins.plugins))

    async def _plugin_metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                self.plugin_metrics = await self.plugins.collect_metrics(None)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        if self.finished:
            return
        self.finished = True
        await self.controller.stop(hangup=True)
        await asyncio.sleep(0.3)
        self.controller._reap_finished()
        if self._plugin_task:
            self._plugin_task.cancel()
        await self.engine.shutdown()  # runs plugins.post_run via shutdown hooks
        if self.monitor is not None:
            try:
                await self.monitor.sample()  # final PBX-side point after hangup
            except Exception:  # noqa: BLE001
                pass
            await self.monitor.stop()
        if self.adapter is not None:
            await self.adapter.close()
        if self._persist_task:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
        self._persist(final=True)

    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                self._persist()
            except Exception:  # noqa: BLE001
                log.exception("persist failed")

    def _persist(self, final: bool = False) -> None:
        if self.store is None or self.run_id is None:
            return
        samples = list(self.stats.series)[self._persisted_samples :]
        self.store.add_samples(self.run_id, samples)
        self._persisted_samples = len(self.stats.series)
        if self.monitor is not None:
            mon = [{"kind": "monitor", **{k: v for k, v in p.items() if k not in ("top_threads", "threads_by_name")}} for p in list(self.monitor.state.series)[self._persisted_monitor :]]
            self.store.add_samples(self.run_id, [dict(m, t=round(m["t"] - self.stats.t0, 1)) for m in mon])
            self._persisted_monitor = len(self.monitor.state.series)
        events = list(self.stats.events)
        new_events = events[self._persisted_events :] if len(events) >= self._persisted_events else events
        self.store.add_events(self.run_id, new_events)
        self._persisted_events = len(events)
        finished = list(self.stats.finished_calls)
        new_calls = finished[self._persisted_calls :] if len(finished) >= self._persisted_calls else finished
        self.store.add_calls(self.run_id, new_calls)
        self._persisted_calls = len(finished)
        if final:
            self.store.finish_run(self.run_id, self.summary())

    # -- reading ------------------------------------------------------------------------

    def summary(self) -> dict:
        s = self.stats.summary()
        s["name"] = self.name
        s["run_id"] = self.run_id
        s["scenario"] = self.scenario.name
        s["pbx"] = {"host": self.scenario.pbx.host, "domain": self.scenario.pbx.domain, "environment": self.scenario.pbx.environment}
        s["controller"] = self.controller.state()
        s["pbx_profile"] = self.profile.name if self.profile else None
        if self.plugins is not None and len(self.plugins):
            s["plugins"] = self.plugins.snapshot()
            s["plugin_rows"] = self.plugins.report_rows()
        if self.monitor is not None:
            m = self.monitor.snapshot(series_tail=0, log_tail=0)
            s["monitor"] = {"samples": m["samples"], "last": m["last"], "threads_by_name": m["threads_by_name"], "log_level_counts": m["log_level_counts"], "esl_events": m["esl_events"], "hangup_causes": m["hangup_causes"], "errors": m["errors"]}
        elif self.monitor_error:
            s["monitor"] = {"error": self.monitor_error}
        return s

    def snapshot(self, series_tail: int = 900, calls_tail: int = 60) -> dict:
        active = [c.record for c in self.engine.calls if c.record.state != CallState.DONE]
        recent_done = [c.record for c in self.engine.calls if c.record.state == CallState.DONE][-calls_tail:]
        regs = {}
        if self.engine.answerer is not None:
            for u, e in self.engine.answerer.extensions.items():
                regs[u] = {"state": e.registration.state if e.registration else "n/a", "active": e.active_calls, "max_calls": e.max_calls, "busy_rejects": e.rejected_busy}
        return {
            "name": self.name,
            "run_id": self.run_id,
            "finished": self.finished,
            "scenario": self.scenario.name,
            "pbx": {"host": self.scenario.pbx.host, "domain": self.scenario.pbx.domain, "environment": self.scenario.pbx.environment},
            "controller": self.controller.state(),
            "stats": self.stats.summary(),
            "series": list(self.stats.series)[-series_tail:],
            "events": list(self.stats.events)[-40:],
            "calls": [self._call_row(r) for r in (active + recent_done)],
            "registrations": regs,
            "media": self.engine.media_engine.describe() if self.engine.media_engine else {},
            "pbx_profile": self.profile.public() if self.profile else None,
            "monitor": self._monitor_snapshot(series_tail),
            "plugins": self.plugins.snapshot() if self.plugins is not None and len(self.plugins) else {},
            "plugin_metrics": self.plugin_metrics if self.monitor is None else (self.monitor.state.last.get("custom") or {}),
        }

    def _monitor_snapshot(self, series_tail: int) -> dict | None:
        if self.monitor is None:
            return {"error": self.monitor_error} if self.monitor_error else None
        m = self.monitor.snapshot(series_tail=series_tail, log_tail=80)
        t0 = self.stats.t0
        for p in m["series"]:
            p["t"] = round(p["t"] - t0, 1)
        return m

    @staticmethod
    def _call_row(r: CallRecord) -> dict:
        m = r.media
        return {
            "id": r.id,
            "state": r.state.value,
            "remote": r.remote_user,
            "reason": r.end_reason,
            "status": r.final_status,
            "to200_ms": r.invite_to_200_ms,
            "duration_s": r.duration_s,
            "tx": m.tx_packets if m else 0,
            "rx": m.rx_packets if m else 0,
            "lost": m.rx_lost if m else 0,
            "late_max_ms": round(m.tx_late_max * 1000, 2) if m else 0,
        }

    def steps_from_text(self, text: str) -> list[ScheduleStep]:
        from semishigure.core.controller import parse_schedule

        return parse_schedule(text, self.config.call_duration)
