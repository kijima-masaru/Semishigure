"""Run statistics: counters, response-time percentiles and a 1 s time series."""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from semishigure.core.call import CallRecord, CallState


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


@dataclass
class RunStats:
    started_at: float = field(default_factory=time.time)
    t0: float = field(default_factory=time.monotonic)
    calls_started: int = 0
    calls_established: int = 0
    calls_failed: int = 0
    calls_ended: int = 0
    fail_by_status: Counter = field(default_factory=Counter)
    end_by_reason: Counter = field(default_factory=Counter)
    invite_to_200: list[float] = field(default_factory=list)
    invite_to_180: list[float] = field(default_factory=list)
    rtp_late_max_ms: float = 0.0
    rtp_late_over_5ms: int = 0
    rtp_tx_packets: int = 0
    rtp_rx_packets: int = 0
    rtp_rx_lost: int = 0
    answered_by_ext: Counter = field(default_factory=Counter)
    answerer_end_by_reason: Counter = field(default_factory=Counter)
    answerer_busy_rejects: int = 0
    finished_calls: deque = field(default_factory=lambda: deque(maxlen=20000))  # summaries, both roles
    series: deque = field(default_factory=lambda: deque(maxlen=14400))
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    _recent_starts: deque = field(default_factory=lambda: deque(maxlen=600))
    _seen_done: set = field(default_factory=set)

    # -- feeding -------------------------------------------------------------------

    def call_started(self) -> None:
        self.calls_started += 1
        self._recent_starts.append(time.monotonic())

    def call_established(self, rec: CallRecord) -> None:
        """Called when an outbound call is answered (response times are known now)."""
        self.calls_established += 1
        if rec.invite_to_200_ms is not None:
            self.invite_to_200.append(rec.invite_to_200_ms)
        if rec.invite_to_180_ms is not None:
            self.invite_to_180.append(rec.invite_to_180_ms)

    def call_finished(self, rec: CallRecord) -> None:
        """Called once per outbound call when it reaches DONE."""
        if rec.id in self._seen_done:
            return
        self._seen_done.add(rec.id)
        self.finished_calls.append(rec.summary())
        self.calls_ended += 1
        if rec.t_established is not None:
            self.end_by_reason[rec.end_reason] += 1
        else:
            self.calls_failed += 1
            self.fail_by_status[str(rec.final_status or rec.end_reason)] += 1
        m = rec.media
        if m is not None:
            self.rtp_late_max_ms = max(self.rtp_late_max_ms, m.tx_late_max * 1000)
            self.rtp_late_over_5ms += m.tx_late_over_5ms
            self.rtp_tx_packets += m.tx_packets
            self.rtp_rx_packets += m.rx_packets
            self.rtp_rx_lost += m.rx_lost

    def answerer_call_finished(self, rec: CallRecord) -> None:
        if rec.id in self._seen_done:
            return
        self._seen_done.add(rec.id)
        self.finished_calls.append(rec.summary())
        self.answerer_end_by_reason[rec.end_reason] += 1
        if rec.t_established is not None:
            self.answered_by_ext[rec.extension] += 1
            m = rec.media
            if m is not None:
                self.rtp_late_max_ms = max(self.rtp_late_max_ms, m.tx_late_max * 1000)
                self.rtp_late_over_5ms += m.tx_late_over_5ms

    def event(self, kind: str, message: str, **data) -> dict:
        ev = {"t": round(time.monotonic() - self.t0, 1), "wall": time.time(), "kind": kind, "message": message, **data}
        self.events.append(ev)
        return ev

    def calls_per_second(self, window: float = 5.0) -> float:
        now = time.monotonic()
        n = sum(1 for t in self._recent_starts if now - t <= window)
        return round(n / window, 2)

    def sample(self, target: int, established: int, pending: int, extra: dict | None = None) -> dict:
        point = {
            "t": round(time.monotonic() - self.t0, 1),
            "wall": time.time(),
            "target": target,
            "established": established,
            "pending": pending,
            "failed": self.calls_failed,
            "cps": self.calls_per_second(),
            "rtp_late_max_ms": round(self.rtp_late_max_ms, 2),
        }
        if extra:
            point.update(extra)
        self.series.append(point)
        return point

    # -- reading -----------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "started_at": self.started_at,
            "elapsed_s": round(time.monotonic() - self.t0, 1),
            "calls_started": self.calls_started,
            "calls_established": self.calls_established,
            "calls_failed": self.calls_failed,
            "calls_ended": self.calls_ended,
            "fail_by_status": dict(self.fail_by_status),
            "end_by_reason": dict(self.end_by_reason),
            "answered_by_ext": dict(self.answered_by_ext),
            "answerer_end_by_reason": dict(self.answerer_end_by_reason),
            "answerer_busy_rejects": self.answerer_busy_rejects,
            "invite_to_200_ms": {"p50": percentile(self.invite_to_200, 0.5), "p95": percentile(self.invite_to_200, 0.95), "max": max(self.invite_to_200) if self.invite_to_200 else None, "n": len(self.invite_to_200)},
            "invite_to_180_ms": {"p50": percentile(self.invite_to_180, 0.5), "p95": percentile(self.invite_to_180, 0.95), "n": len(self.invite_to_180)},
            "rtp": {"late_max_ms": round(self.rtp_late_max_ms, 2), "late_over_5ms": self.rtp_late_over_5ms, "tx_packets": self.rtp_tx_packets, "rx_packets": self.rtp_rx_packets, "rx_lost": self.rtp_rx_lost},
            "cps": self.calls_per_second(),
        }


def classify(records: list[CallRecord]) -> tuple[list[CallRecord], list[CallRecord]]:
    """Split active outbound records into (established, pending)."""
    established = [r for r in records if r.state == CallState.ESTABLISHED]
    pending = [r for r in records if r.state in (CallState.IDLE, CallState.INVITING, CallState.AUTH, CallState.RINGING)]
    return established, pending
