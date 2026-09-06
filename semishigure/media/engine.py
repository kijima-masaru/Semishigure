"""Pure-Python media engine: UDP sockets + pump threads that tick every 20 ms.

Each pump thread handles up to ``max_sessions_per_pump`` sessions; a new
thread is started automatically beyond that. A single receive thread drains
all sockets with a selector and keeps per-session statistics.
"""

from __future__ import annotations

import logging
import random
import selectors
import socket
import threading
import time
from pathlib import Path

from semishigure.media import codec as g711
from semishigure.media.base import MediaEngine, MediaSession, MediaStats
from semishigure.media.rtp import build_packet, parse_packet
from semishigure.media.wav import FRAME_SAMPLES, AudioSource, write_wav

log = logging.getLogger(__name__)

TICK = 0.020
RX_LEVEL_EVERY = 5  # decode 1 in N received packets for the level meter
MAX_RECORD_SECONDS = 900
_CODEC_BY_PT = {0: "PCMU", 8: "PCMA"}


class PythonMediaSession(MediaSession):
    def __init__(self, engine: PythonMediaEngine, sock: socket.socket, local_ip: str, source: AudioSource | None, record_rx: Path | None):
        self.engine = engine
        self.sock = sock
        self.local_ip = local_ip
        self.local_port = sock.getsockname()[1]
        self.source = source
        self.record_path = record_rx
        self._record = bytearray() if record_rx else None
        self._stats = MediaStats()
        self.remote: tuple[str, int] | None = None
        self.codec: str | None = None
        self.payload_type = 0
        self.payload: bytes = b""
        self.frame_index = 0
        self.total_frames = 0
        self.seq = random.randint(0, 0xFFFF)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(1, 0xFFFFFFFF)
        self.sending = False
        self.closed = False
        self._marker_pending = True
        self._last_rx_seq: int | None = None
        self._rx_count_for_level = 0
        self._lock = threading.Lock()

    # -- MediaSession ---------------------------------------------------------

    def set_remote(self, ip: str, port: int, codec: str, payload_type: int) -> None:
        with self._lock:
            self.remote = (ip, port)
            self.codec = codec
            self.payload_type = payload_type
            self._stats.codec = codec
            if self.source is not None:
                self.payload = self.source.payload(codec)
                self.total_frames = len(self.payload) // FRAME_SAMPLES

    def start(self) -> None:
        if self.sending or self.closed:
            return
        if self.source is None or self.remote is None or self.total_frames == 0:
            return
        self.sending = True
        self.engine._attach_to_pump(self)

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.sending = False
        self.engine._detach(self)
        if self._record is not None and self.record_path is not None:
            try:
                write_wav(self.record_path, bytes(self._record))
            except OSError as exc:
                log.warning("could not write %s: %s", self.record_path, exc)
        try:
            self.sock.close()
        except OSError:
            pass

    def stats(self) -> MediaStats:
        return self._stats

    # -- called by pump thread --------------------------------------------------

    def _tick(self, now: float, late: float) -> None:
        if not self.sending or self.remote is None:
            return
        start = self.frame_index * FRAME_SAMPLES
        frame = self.payload[start : start + FRAME_SAMPLES]
        self.frame_index += 1
        if self.frame_index >= self.total_frames:
            if self.source is not None and self.source.loop:
                self.frame_index = 0
            else:
                self.sending = False
        pkt = build_packet(self.payload_type, self.seq, self.timestamp, self.ssrc, frame, marker=self._marker_pending)
        self._marker_pending = False
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + FRAME_SAMPLES) & 0xFFFFFFFF
        try:
            self.sock.sendto(pkt, self.remote)
        except OSError as exc:
            log.debug("rtp send error to %s: %s", self.remote, exc)
            return
        st = self._stats
        st.tx_packets += 1
        if st.first_tx_at is None:
            st.first_tx_at = now
        if late > st.tx_late_max:
            st.tx_late_max = late
        st.tx_late_sum += late
        if late > 0.005:
            st.tx_late_over_5ms += 1
        if late > 0.020:
            st.tx_late_over_20ms += 1
        if not self.sending:
            self.engine._detach_from_pump(self)

    # -- called by rx thread ----------------------------------------------------

    def _on_datagram(self, data: bytes, now: float) -> None:
        pkt = parse_packet(data)
        if pkt is None:
            return
        st = self._stats
        st.rx_packets += 1
        st.rx_bytes += len(pkt.payload)
        st.last_rx_at = now
        if st.first_rx_at is None:
            st.first_rx_at = now
            st.rx_ssrc = pkt.ssrc
        if self._last_rx_seq is not None:
            gap = (pkt.seq - self._last_rx_seq) & 0xFFFF
            if 1 < gap < 0x8000:
                st.rx_lost += gap - 1
        self._last_rx_seq = pkt.seq
        codec = _CODEC_BY_PT.get(pkt.payload_type)
        if codec is None:
            return  # telephone-event or unknown payload
        want_decode = self._record is not None
        self._rx_count_for_level += 1
        if self._rx_count_for_level % RX_LEVEL_EVERY == 0:
            want_decode = True
        if not want_decode:
            return
        pcm = g711.decode(codec, pkt.payload)
        if self._rx_count_for_level % RX_LEVEL_EVERY == 0:
            rms = g711.rms_pcm16(pcm)
            st.rx_level_rms = 0.7 * st.rx_level_rms + 0.3 * rms
            if st.rx_level_rms > st.rx_level_peak_rms:
                st.rx_level_peak_rms = st.rx_level_rms
        if self._record is not None and len(self._record) < MAX_RECORD_SECONDS * 16000:
            self._record.extend(pcm)


class _Pump(threading.Thread):
    def __init__(self, index: int):
        super().__init__(name=f"media-pump-{index}", daemon=True)
        self.sessions: list[PythonMediaSession] = []
        self.lock = threading.Lock()
        self.running = True
        self.ticks = 0
        self.late_max = 0.0
        self.resyncs = 0

    def add(self, s: PythonMediaSession) -> None:
        with self.lock:
            if s not in self.sessions:
                self.sessions.append(s)

    def remove(self, s: PythonMediaSession) -> None:
        with self.lock:
            if s in self.sessions:
                self.sessions.remove(s)

    def __len__(self) -> int:
        return len(self.sessions)

    def run(self) -> None:
        next_t = time.monotonic() + TICK
        while self.running:
            now = time.monotonic()
            if now < next_t:
                time.sleep(next_t - now)
                now = time.monotonic()
            late = now - next_t
            if late > self.late_max:
                self.late_max = late
            with self.lock:
                sessions = list(self.sessions)
            for s in sessions:
                try:
                    s._tick(time.monotonic(), late)
                except Exception:  # noqa: BLE001
                    log.exception("pump error")
            self.ticks += 1
            next_t += TICK
            if now - next_t > 1.0:  # we were suspended; do not burst-catch-up
                self.resyncs += 1
                next_t = now + TICK


class _Receiver(threading.Thread):
    def __init__(self):
        super().__init__(name="media-rx", daemon=True)
        self.selector = selectors.DefaultSelector()
        self.pending: list[tuple[str, PythonMediaSession]] = []
        self.lock = threading.Lock()
        self.running = True
        self.datagrams = 0

    def register(self, s: PythonMediaSession) -> None:
        with self.lock:
            self.pending.append(("add", s))

    def unregister(self, s: PythonMediaSession) -> None:
        with self.lock:
            self.pending.append(("del", s))

    def _apply_pending(self) -> None:
        with self.lock:
            ops, self.pending = self.pending, []
        for op, s in ops:
            try:
                if op == "add":
                    self.selector.register(s.sock, selectors.EVENT_READ, s)
                else:
                    self.selector.unregister(s.sock)
            except (KeyError, ValueError, OSError):
                pass

    def run(self) -> None:
        while self.running:
            self._apply_pending()
            try:
                events = self.selector.select(timeout=0.05)
            except OSError:
                continue
            now = time.monotonic()
            for key, _ in events:
                s: PythonMediaSession = key.data
                for _ in range(64):
                    try:
                        data, _addr = s.sock.recvfrom(2048)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    self.datagrams += 1
                    s._on_datagram(data, now)


class PythonMediaEngine(MediaEngine):
    def __init__(self, port_start: int = 20000, port_end: int = 20999, max_sessions_per_pump: int = 50, bind_ip: str = "0.0.0.0"):
        self.port_start = port_start
        self.port_end = port_end
        self.max_sessions_per_pump = max_sessions_per_pump
        self.bind_ip = bind_ip
        self._next_port = port_start
        self._pumps: list[_Pump] = []
        self._rx = _Receiver()
        self._rx.start()
        self._sessions: set[PythonMediaSession] = set()
        self._lock = threading.Lock()

    def _alloc_socket(self) -> socket.socket:
        for _ in range((self.port_end - self.port_start) // 2 + 1):
            port = self._next_port
            self._next_port += 2
            if self._next_port > self.port_end:
                self._next_port = self.port_start
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((self.bind_ip, port))
            except OSError:
                sock.close()
                continue
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            return sock
        raise RuntimeError("no free RTP port")

    def create_session(self, local_ip: str, source: AudioSource | None, record_rx: Path | None = None) -> MediaSession:
        with self._lock:
            sock = self._alloc_socket()
            s = PythonMediaSession(self, sock, local_ip, source, record_rx)
            self._sessions.add(s)
        self._rx.register(s)
        return s

    def _attach_to_pump(self, s: PythonMediaSession) -> None:
        with self._lock:
            for p in self._pumps:
                if len(p) < self.max_sessions_per_pump:
                    p.add(s)
                    return
            p = _Pump(len(self._pumps))
            self._pumps.append(p)
            p.add(s)
            p.start()
            log.info("started %s", p.name)

    def _detach_from_pump(self, s: PythonMediaSession) -> None:
        for p in self._pumps:
            p.remove(s)

    def _detach(self, s: PythonMediaSession) -> None:
        self._detach_from_pump(s)
        self._rx.unregister(s)
        with self._lock:
            self._sessions.discard(s)

    def close(self) -> None:
        for s in list(self._sessions):
            s.stop()
        for p in self._pumps:
            p.running = False
        self._rx.running = False

    def describe(self) -> dict:
        return {
            "engine": "python",
            "pumps": [
                {"name": p.name, "sessions": len(p), "ticks": p.ticks, "late_max_ms": round(p.late_max * 1000, 2), "resyncs": p.resyncs}
                for p in self._pumps
            ],
            "rx_datagrams": self._rx.datagrams,
            "active_sessions": len(self._sessions),
        }
