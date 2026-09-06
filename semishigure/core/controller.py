"""Load controller (design §3.4): keeps the number of concurrent calls at a
target that can be changed while running.

Every 100 ms:
    established = calls in ESTABLISHED
    pending     = calls in INVITING/AUTH/RINGING
    deficit     = target - (established + pending)
    deficit > 0 -> start new calls, rate-limited by ramp_rate (token bucket)
    deficit < 0 -> hang up the oldest calls, rate-limited by drain_rate
    calls older than call_duration are hung up and replaced by the loop
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from semishigure.core.call import CallRecord, CallState
from semishigure.core.engine import SipEngine
from semishigure.core.stats import RunStats, classify
from semishigure.sip.uac import OutboundCall

log = logging.getLogger(__name__)

TICK = 0.1
BACKOFF_STATUSES = {486, 503, 480, 408}


@dataclass
class ControllerConfig:
    target: int = 0
    ramp_rate: float = 0.5  # new calls per second
    drain_rate: float = 5.0  # hangups per second when lowering the target
    call_duration: float = 180.0
    max_concurrency: int = 50  # absolute cap (app setting)
    max_total_calls: int | None = None
    backoff_threshold: int = 3  # consecutive 486/503 before lowering the target
    backoff_enabled: bool = True
    # A call that just ended still occupies PBX resources (limit counters, channel
    # teardown) for a moment; count it for this long before replacing it.
    teardown_grace: float = 0.5


@dataclass
class ScheduleStep:
    target: int
    seconds: float
    ramp_rate: float | None = None
    burst: bool = False


@dataclass
class LoadController:
    engine: SipEngine
    config: ControllerConfig
    stats: RunStats
    on_change: Callable[[str, dict], None] | None = None
    target: int = 0
    paused: bool = False
    burst: bool = False
    running: bool = False
    stopped: bool = False
    backoff_reason: str = ""
    schedule: list[ScheduleStep] = field(default_factory=list)
    schedule_index: int = -1
    schedule_step_started: float | None = None
    _tokens: float = 0.0
    _drain_tokens: float = 0.0
    _last_tick: float = 0.0
    _last_sample: float = 0.0
    _consecutive_failures: int = 0
    _recent_ended: list[float] = field(default_factory=list)
    _hanging_up: set = field(default_factory=set)
    _task: asyncio.Task | None = None
    _schedule_task: asyncio.Task | None = None
    _call_tasks: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.target = min(self.config.target, self.config.max_concurrency)

    # -- control -------------------------------------------------------------------

    def _notify(self, kind: str, message: str, **data) -> None:
        ev = self.stats.event(kind, message, **data)
        if self.on_change:
            try:
                self.on_change(kind, ev)
            except Exception:  # noqa: BLE001
                pass

    def set_target(self, n: int, source: str = "api") -> int:
        n = max(0, min(int(n), self.config.max_concurrency))
        if n != self.target:
            self.target = n
            self.backoff_reason = ""
            self._notify("target", f"target -> {n} ({source})", target=n)
        return self.target

    def adjust(self, delta: int, source: str = "api") -> int:
        return self.set_target(self.target + delta, source)

    def set_burst(self, on: bool = True) -> None:
        self.burst = on
        self._notify("burst", "burst on" if on else "burst off")

    def pause(self) -> None:
        self.paused = True
        self._notify("pause", "paused: no new calls")

    def resume(self) -> None:
        self.paused = False
        self._notify("resume", "resumed")

    async def hangup_all(self) -> None:
        self.set_target(0, "hangup_all")
        self.clear_schedule()
        await self.engine.hangup_all("hangup_all")
        self._notify("hangup_all", "all calls hung up")

    def set_ramp_rate(self, rate: float) -> None:
        self.config.ramp_rate = max(0.01, float(rate))
        self._notify("ramp", f"ramp_rate -> {self.config.ramp_rate}/s")

    def set_call_duration(self, seconds: float) -> None:
        self.config.call_duration = max(1.0, float(seconds))
        self._notify("duration", f"call_duration -> {self.config.call_duration}s")

    # -- schedule (pattern A style steps) ---------------------------------------------

    def run_schedule(self, steps: list[ScheduleStep], then_target: int | None = 0) -> None:
        self.clear_schedule()
        self.schedule = list(steps)
        self._schedule_task = asyncio.get_running_loop().create_task(self._schedule_loop(then_target))

    def clear_schedule(self) -> None:
        if self._schedule_task and not self._schedule_task.done():
            self._schedule_task.cancel()
        self._schedule_task = None
        self.schedule = []
        self.schedule_index = -1
        self.schedule_step_started = None

    async def _schedule_loop(self, then_target: int | None) -> None:
        try:
            for i, step in enumerate(self.schedule):
                self.schedule_index = i
                self.schedule_step_started = time.monotonic()
                if step.ramp_rate is not None:
                    self.config.ramp_rate = step.ramp_rate
                self.burst = step.burst
                self.set_target(step.target, f"schedule step {i + 1}/{len(self.schedule)}")
                await asyncio.sleep(step.seconds)
            self.burst = False
            if then_target is not None:
                self.set_target(then_target, "schedule end")
            self._notify("schedule", "schedule finished")
        except asyncio.CancelledError:
            pass
        finally:
            self.schedule_index = -1
            self.schedule_step_started = None

    # -- loop -------------------------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._last_tick = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="load-controller")
        self._notify("start", f"controller started target={self.target} ramp={self.config.ramp_rate}/s duration={self.config.call_duration}s")

    async def stop(self, hangup: bool = True) -> None:
        self.stopped = True
        self.running = False
        self.clear_schedule()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if hangup:
            await self.engine.hangup_all("stop")
        for t in list(self._call_tasks):
            if not t.done():
                t.cancel()
        self._notify("stop", "controller stopped")

    @property
    def active_records(self) -> list[CallRecord]:
        return [c.record for c in self.engine.calls if c.record.state != CallState.DONE]

    async def _loop(self) -> None:
        while self.running:
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("controller tick failed")
            await asyncio.sleep(TICK)

    def _tick(self) -> None:
        if self.engine.is_shutdown:
            self.running = False
            return
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        cfg = self.config
        self._reap_finished()
        established, pending = classify(self.active_records)

        # 1. expire calls that exceeded call_duration
        for rec in established:
            if rec.t_established is not None and now - rec.t_established >= cfg.call_duration:
                self._hangup(rec, "duration_elapsed")

        established = [r for r in established if r.state == CallState.ESTABLISHED and r.id not in self._hanging_up]
        self._recent_ended = [t for t in self._recent_ended if now - t < cfg.teardown_grace]
        terminating = sum(1 for r in self.active_records if r.state == CallState.TERMINATING or r.id in self._hanging_up)
        occupied = len(established) + len(pending) + terminating
        # recently ended calls only delay refills; they never justify draining
        deficit = self.target - occupied - len(self._recent_ended)
        excess_now = occupied - self.target

        # 2. ramp up
        self._tokens = min(self._tokens + cfg.ramp_rate * dt, max(1.0, cfg.ramp_rate * 2))
        if deficit > 0 and not self.paused:
            if cfg.max_total_calls is not None:
                deficit = min(deficit, cfg.max_total_calls - self.stats.calls_started)
            n = deficit if self.burst else min(deficit, int(self._tokens))
            for _ in range(max(0, n)):
                self._start_call()
                if not self.burst:
                    self._tokens -= 1
        # 3. drain (oldest first)
        elif excess_now > 0:
            self._drain_tokens = min(self._drain_tokens + cfg.drain_rate * dt, cfg.drain_rate)
            excess = min(excess_now, int(self._drain_tokens))
            candidates = [r for r in established if r.id not in self._hanging_up]
            victims = sorted(candidates, key=lambda r: r.t_established or 0)[:excess]
            remaining = excess - len(victims)
            if remaining > 0:
                victims += sorted(pending, key=lambda r: r.t_invite or 0)[:remaining]
            for rec in victims:
                self._hangup(rec, "drain")
                self._drain_tokens -= 1

        # 4. sample once per second
        if now - self._last_sample >= 1.0:
            self._last_sample = now
            extra = {}
            if self.engine.media_engine is not None:
                desc = self.engine.media_engine.describe()
                extra["pump_late_max_ms"] = max((p["late_max_ms"] for p in desc.get("pumps", [])), default=0.0)
                extra["media_sessions"] = desc.get("active_sessions", 0)
            self.stats.sample(self.target, len(established), len(pending), extra)

    def _start_call(self) -> None:
        call = self.engine.new_call()
        self.stats.call_started()
        task = asyncio.get_running_loop().create_task(self._run_call(call))
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)

    async def _run_call(self, call: OutboundCall) -> None:
        try:
            rec = await call.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("call failed to start")
            call.record.finish(f"exception:{exc.__class__.__name__}")
            return
        if rec.state == CallState.ESTABLISHED:
            self._consecutive_failures = 0
            self.stats.call_established(rec)
            return
        # failed before being established
        self._notify("call_failed", f"call {rec.id} failed: {rec.end_reason}", status=rec.final_status, reason=rec.end_reason)
        if rec.final_status in BACKOFF_STATUSES:
            self._consecutive_failures += 1
            if self.config.backoff_enabled and self._consecutive_failures >= self.config.backoff_threshold:
                established, _ = classify(self.active_records)
                new_target = max(0, min(self.target - 1, len(established)))
                self.backoff_reason = f"{self._consecutive_failures} consecutive {rec.final_status}; target lowered to {new_target}"
                self._consecutive_failures = 0
                self.target = new_target
                self._notify("backoff", self.backoff_reason, target=new_target, status=rec.final_status)
        else:
            self._consecutive_failures = 0

    def _hangup(self, rec: CallRecord, reason: str) -> None:
        if rec.id in self._hanging_up or rec.state in (CallState.TERMINATING, CallState.DONE):
            return
        for call in self.engine.calls:
            if call.record is rec:
                self._hanging_up.add(rec.id)
                task = asyncio.get_running_loop().create_task(call.hangup(reason))
                self._call_tasks.add(task)
                task.add_done_callback(self._call_tasks.discard)
                task.add_done_callback(lambda _t, rid=rec.id: self._hanging_up.discard(rid))
                return

    def _reap_finished(self) -> None:
        """Move finished calls into stats and drop them from the engine list."""
        keep: list[OutboundCall] = []
        for call in self.engine.calls:
            if call.record.state == CallState.DONE:
                self.stats.call_finished(call.record)
                # established or not, the PBX needs a moment to tear the leg down
                self._recent_ended.append(call.record.t_ended or time.monotonic())
            else:
                keep.append(call)
        self.engine.calls = keep
        if self.engine.answerer is not None:
            done = [r for r in self.engine.answerer.records if r.state == CallState.DONE]
            for r in done:
                self.stats.answerer_call_finished(r)
            self.stats.answerer_busy_rejects = sum(e.rejected_busy for e in self.engine.answerer.extensions.values())
            if len(self.engine.answerer.records) > 2000:
                self.engine.answerer.records = [r for r in self.engine.answerer.records if r.state != CallState.DONE]

    # -- reading -------------------------------------------------------------------------

    def state(self) -> dict:
        established, pending = classify(self.active_records)
        step = None
        if 0 <= self.schedule_index < len(self.schedule):
            s = self.schedule[self.schedule_index]
            step = {"index": self.schedule_index, "count": len(self.schedule), "target": s.target, "seconds": s.seconds, "elapsed": round(time.monotonic() - (self.schedule_step_started or time.monotonic()), 1)}
        return {
            "running": self.running,
            "paused": self.paused,
            "burst": self.burst,
            "target": self.target,
            "established": len(established),
            "pending": len(pending),
            "ramp_rate": self.config.ramp_rate,
            "drain_rate": self.config.drain_rate,
            "call_duration": self.config.call_duration,
            "max_concurrency": self.config.max_concurrency,
            "backoff_reason": self.backoff_reason,
            "schedule_step": step,
            "schedule": [{"target": s.target, "seconds": s.seconds} for s in self.schedule],
        }


def parse_schedule(text: str, default_seconds: float = 180.0) -> list[ScheduleStep]:
    """'5:60,20:60,8:60' or '5,10,20' (each default_seconds) -> steps."""
    steps: list[ScheduleStep] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            n, secs = part.split(":", 1)
            steps.append(ScheduleStep(int(n), float(secs)))
        else:
            steps.append(ScheduleStep(int(part), default_seconds))
    return steps


def preset_schedule(preset: dict, default_duration: float) -> tuple[list[ScheduleStep], float | None, bool]:
    """Design §3.3 presets: A: target [5,10,20] each call_duration; B: burst."""
    from semishigure.scenario.model import parse_duration, parse_rate

    targets = preset.get("target_concurrency", 0)
    seconds = parse_duration(preset.get("step_seconds", preset.get("call_duration")), default_duration)
    ramp = parse_rate(preset.get("ramp_rate"), 0.5) if preset.get("ramp_rate") is not None else None
    burst = bool(preset.get("burst", False)) or (ramp is not None and ramp >= 10)
    if isinstance(targets, list):
        steps = [ScheduleStep(int(t), seconds, ramp, burst) for t in targets]
    else:
        steps = [ScheduleStep(int(targets), seconds, ramp, burst)]
    return steps, ramp, burst
