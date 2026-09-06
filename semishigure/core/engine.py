"""SipEngine: wires scenario -> endpoints, media engine, answerer and outbound calls.

Also owns the safety net: ``shutdown()`` hangs up every call (BYE/CANCEL),
unregisters every extension and releases sockets/threads. It is idempotent
and is wired to SIGINT/SIGTERM and to the CLI's ``finally``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from semishigure.core.call import CallRecord, CallRole, CallState
from semishigure.media.base import MediaEngine
from semishigure.media.engine import PythonMediaEngine
from semishigure.media.wav import AudioSource, load_wav, synth_speech_like
from semishigure.scenario.model import Scenario
from semishigure.secrets import SecretStore
from semishigure.sip.endpoint import SipEndpoint
from semishigure.sip.transport import discover_local_ip
from semishigure.sip.uac import OutboundCall
from semishigure.sip.uas import Answerer, AnswererExtension

log = logging.getLogger(__name__)

EventCallback = Callable[[CallRecord, str], None]


class SipEngine:
    def __init__(
        self,
        scenario: Scenario,
        secrets: SecretStore | None = None,
        *,
        media_engine: MediaEngine | None = None,
        record_rx_dir: Path | None = None,
        sip_trace: bool = False,
        on_event: EventCallback | None = None,
        register_expires: int = 300,
    ):
        self.scenario = scenario
        self.secrets = secrets or SecretStore()
        self.record_rx_dir = record_rx_dir
        self.sip_trace = sip_trace
        self.on_event = on_event
        self.register_expires = register_expires
        self.local_ip: str = scenario.pbx.local_ip or ""
        self.pbx_addr = (scenario.pbx.host, scenario.pbx.sip_port)
        self.media_engine: MediaEngine | None = media_engine
        self.caller_endpoint: SipEndpoint | None = None
        self.answerer_endpoint: SipEndpoint | None = None
        self.answerer: Answerer | None = None
        self.caller_audio: AudioSource | None = None
        self.answerer_audio: AudioSource | None = None
        self.calls: list[OutboundCall] = []
        self.registration_results: dict[str, bool] = {}
        self._started = False
        self._shutdown_done = False
        self._shutdown_lock = asyncio.Lock()
        self._caller_password = ""

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        sc = self.scenario
        if not self.local_ip:
            self.local_ip = discover_local_ip(sc.pbx.host, sc.pbx.sip_port)
        if self.media_engine is None:
            self.media_engine = PythonMediaEngine(sc.pbx.rtp_port_start, sc.pbx.rtp_port_end)
        self.caller_audio = self._load_audio(sc.caller.audio, sc.caller.audio_loop, seed=1)
        self.answerer_audio = self._load_audio(sc.answerer.audio, sc.answerer.audio_loop, seed=2)
        self._caller_password = self.secrets.resolve(sc.caller.auth_password_ref)

        self.caller_endpoint = SipEndpoint(self.local_ip, sc.pbx.caller_port, trace=self.sip_trace)
        self.answerer_endpoint = SipEndpoint(self.local_ip, sc.pbx.answerer_port, trace=self.sip_trace)
        await self.caller_endpoint.start()
        await self.answerer_endpoint.start()

        extensions = [
            AnswererExtension(
                user=e.user,
                password=self.secrets.resolve(e.password_ref),
                max_calls=e.max_calls,
                audio=self.answerer_audio,
                answer_after=sc.answerer.answer_after,
            )
            for e in sc.answerer.extensions
        ]
        self.answerer = Answerer(
            self.answerer_endpoint,
            self.media_engine,
            self.pbx_addr,
            sc.pbx.domain,
            extensions,
            codecs=["PCMU", "PCMA"],
            record_rx_dir=self.record_rx_dir,
            on_call=self.on_event,
            register_expires=self.register_expires,
            ring_window=sc.answerer.ring_window,
            loser_grace=sc.answerer.loser_grace,
        )
        self._install_signal_handlers()
        self._started = True
        self.registration_results = await self.answerer.start()
        failed = [u for u, ok in self.registration_results.items() if not ok]
        if failed:
            log.warning("registration failed for: %s", ", ".join(failed))

    def _load_audio(self, path: str | None, loop: bool, seed: int) -> AudioSource | None:
        if path is None:
            return None
        if path.startswith("synth:"):
            seconds = float(path.split(":", 1)[1] or 30)
            src = synth_speech_like(seconds, base_hz=160.0 if seed == 1 else 230.0, seed=seed)
            src.loop = loop
            return src
        if path == "none":
            return None
        resolved = self.scenario.resolve_path(path)
        assert resolved is not None
        src = load_wav(resolved, loop=loop)
        log.info("loaded %s: %.1fs", resolved, src.duration_s)
        return src

    def _install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: loop.create_task(self._on_signal(s)))
            except (NotImplementedError, RuntimeError):
                pass

    async def _on_signal(self, sig: signal.Signals) -> None:
        log.warning("received %s: hanging up all calls and unregistering", sig.name)
        await self.shutdown()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            if not self._started:
                return
            log.info("shutdown: %d outbound call(s), %d inbound call(s)", len(self.active_calls), len(self.answerer.active_calls) if self.answerer else 0)
            await asyncio.gather(*(c.hangup("shutdown") for c in self.active_calls), return_exceptions=True)
            if self.answerer is not None:
                await self.answerer.stop()
            for ep in (self.caller_endpoint, self.answerer_endpoint):
                if ep is not None:
                    await ep.wait_idle(1.0)
                    ep.close()
            if self.media_engine is not None:
                self.media_engine.close()
            log.info("shutdown complete")

    # -- calls ------------------------------------------------------------------------

    @property
    def active_calls(self) -> list[OutboundCall]:
        return [c for c in self.calls if c.record.state != CallState.DONE]

    def new_call(self, destination: str | None = None, headers: list[tuple[str, str]] | None = None) -> OutboundCall:
        sc = self.scenario
        assert self.caller_endpoint is not None and self.media_engine is not None
        record = CallRecord(role=CallRole.CALLER, local_user=sc.caller.from_number, remote_user=destination or sc.caller.destination, on_event=self.on_event)
        record_path = None
        if self.record_rx_dir is not None:
            record_path = self.record_rx_dir / f"rx_caller_{record.id}.wav"
        call = OutboundCall(
            self.caller_endpoint,
            self.media_engine,
            pbx_addr=self.pbx_addr,
            domain=sc.pbx.domain,
            auth_user=sc.caller.auth_user,
            password=self._caller_password,
            from_user=sc.caller.from_number,
            destination=destination or sc.caller.destination,
            headers=headers if headers is not None else sc.caller.headers,
            codecs=sc.caller.codecs,
            audio=self.caller_audio,
            record_rx=record_path,
            from_display=sc.caller.from_display,
            record=record,
        )
        self.calls.append(call)
        return call

    async def place_call(self, destination: str | None = None) -> OutboundCall:
        call = self.new_call(destination)
        await call.start()
        return call

    async def hangup_all(self, reason: str = "hangup_all") -> None:
        await asyncio.gather(*(c.hangup(reason) for c in self.active_calls), return_exceptions=True)

    # -- reporting ---------------------------------------------------------------------

    def snapshot(self) -> dict:
        outbound = [c.record for c in self.calls]
        inbound = self.answerer.records if self.answerer else []
        return {
            "local_ip": self.local_ip,
            "pbx": {"host": self.pbx_addr[0], "port": self.pbx_addr[1], "domain": self.scenario.pbx.domain},
            "registrations": {
                u: {"state": e.registration.state if e.registration else "n/a", "rtt_ms": e.registration.register_rtt_ms if e.registration else None, "expires": e.registration.granted_expires if e.registration else None, "busy_rejects": e.rejected_busy}
                for u, e in (self.answerer.extensions.items() if self.answerer else {})
            },
            "caller_calls": [r.summary() for r in outbound],
            "answerer_calls": [r.summary() for r in inbound],
            "media": self.media_engine.describe() if self.media_engine else {},
        }
