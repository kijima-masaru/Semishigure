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
from semishigure.scenario.model import Scenario
from semishigure.secrets import SecretStore

log = logging.getLogger(__name__)

ABSOLUTE_MAX_CONCURRENCY = 50


class ProdConfirmationRequired(RuntimeError):
    pass


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
    ):
        self.scenario = scenario
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
        self.config = ControllerConfig(
            target=target if target is not None else scenario.load.target_concurrency,
            ramp_rate=ramp_rate if ramp_rate is not None else scenario.load.ramp_rate,
            call_duration=call_duration if call_duration is not None else scenario.load.call_duration,
            max_concurrency=cap,
            max_total_calls=max_total_calls if max_total_calls is not None else scenario.load.max_total_calls,
        )
        self.engine = SipEngine(scenario, secrets or SecretStore(), record_rx_dir=record_rx_dir, sip_trace=sip_trace, on_event=self._on_call_event)
        self.controller = LoadController(self.engine, self.config, self.stats, on_change=self._on_controller_event)
        self._persist_task: asyncio.Task | None = None
        self._persisted_samples = 0
        self._persisted_events = 0
        self._persisted_calls = 0
        self.finished = False
        self.started_at: float | None = None

    # -- events ------------------------------------------------------------------------

    def _on_call_event(self, rec: CallRecord, event: str) -> None:
        if event.startswith("done:") and rec.role.value == "caller" and rec.t_established is None:
            pass  # failures are reported by the controller
        if self.on_event and event in ("established", "done:remote_bye") and rec.role.value == "caller":
            self.on_event("call", {"id": rec.id, "event": event})

    def _on_controller_event(self, kind: str, ev: dict) -> None:
        if self.on_event:
            self.on_event(kind, ev)

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self, ignore_register_failure: bool = False) -> None:
        self.started_at = time.time()
        await self.engine.start()
        if not all(self.engine.registration_results.values()) and not ignore_register_failure:
            failed = [u for u, ok in self.engine.registration_results.items() if not ok]
            await self.engine.shutdown()
            raise RuntimeError(f"registration failed for {', '.join(failed)}")
        if self.store is not None:
            self.run_id = self.store.create_run(self.name, self.scenario.name, yaml.safe_dump(self.scenario.raw, allow_unicode=True), {"host": self.scenario.pbx.host, "domain": self.scenario.pbx.domain, "environment": self.scenario.pbx.environment})
            self._persist_task = asyncio.get_running_loop().create_task(self._persist_loop())
        self.controller.start()

    async def stop(self) -> None:
        if self.finished:
            return
        self.finished = True
        await self.controller.stop(hangup=True)
        await asyncio.sleep(0.3)
        self.controller._reap_finished()
        await self.engine.shutdown()
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
        }

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
