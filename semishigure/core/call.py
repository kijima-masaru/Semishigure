"""Call records: state machine, timestamps and per-call metrics.

Shared by the caller (UAC) and answerer (UAS) sides. Timestamps are
``time.monotonic()`` values; ``wall_created`` anchors them to wall-clock time.
"""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from semishigure.media.base import MediaStats


class CallState(enum.StrEnum):
    IDLE = "IDLE"
    INVITING = "INVITING"
    AUTH = "AUTH"
    RINGING = "RINGING"
    ESTABLISHED = "ESTABLISHED"
    TERMINATING = "TERMINATING"
    DONE = "DONE"


class CallRole(enum.StrEnum):
    CALLER = "caller"
    ANSWERER = "answerer"


@dataclass
class CallRecord:
    role: CallRole
    local_user: str
    remote_user: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sip_call_id: str = ""
    extension: str = ""  # answerer extension that took the call (UAS side)
    state: CallState = CallState.IDLE
    end_reason: str = ""
    final_status: int | None = None
    codec: str | None = None
    wall_created: float = field(default_factory=time.time)
    t_created: float = field(default_factory=time.monotonic)
    t_invite: float | None = None  # sent (UAC) / received (UAS)
    t_100: float | None = None
    t_180: float | None = None
    t_183: float | None = None
    t_200: float | None = None
    t_ack: float | None = None
    t_established: float | None = None
    t_bye: float | None = None
    t_ended: float | None = None
    auth_rounds: int = 0
    media: MediaStats | None = None
    timeline: list[tuple[float, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)  # custom INVITE headers (caller side)
    on_event: Callable[[CallRecord, str], None] | None = None

    # -- transitions ------------------------------------------------------------

    def mark(self, event: str, ts: float | None = None) -> float:
        ts = ts if ts is not None else time.monotonic()
        self.timeline.append((ts, event))
        if self.on_event:
            try:
                self.on_event(self, event)
            except Exception:  # noqa: BLE001
                pass
        return ts

    def set_state(self, state: CallState, event: str | None = None) -> None:
        self.state = state
        self.mark(event or state.value)

    def finish(self, reason: str, status: int | None = None) -> None:
        if self.state == CallState.DONE:
            return
        self.end_reason = reason
        if status is not None:
            self.final_status = status
        self.t_ended = time.monotonic()
        self.set_state(CallState.DONE, f"done:{reason}")

    # -- derived metrics (ms) ---------------------------------------------------

    @staticmethod
    def _ms(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return round((b - a) * 1000, 1)

    @property
    def invite_to_100_ms(self) -> float | None:
        return self._ms(self.t_invite, self.t_100)

    @property
    def invite_to_180_ms(self) -> float | None:
        return self._ms(self.t_invite, self.t_180)

    @property
    def invite_to_200_ms(self) -> float | None:
        return self._ms(self.t_invite, self.t_200)

    @property
    def established_to_first_rtp_rx_ms(self) -> float | None:
        if self.media is None:
            return None
        return self._ms(self.t_established, self.media.first_rx_at)

    @property
    def established_to_first_rtp_tx_ms(self) -> float | None:
        if self.media is None:
            return None
        return self._ms(self.t_established, self.media.first_tx_at)

    @property
    def duration_s(self) -> float | None:
        if self.t_established is None:
            return None
        end = self.t_ended or time.monotonic()
        return round(end - self.t_established, 2)

    def summary(self) -> dict:
        d = {
            "id": self.id,
            "role": self.role.value,
            "local": self.local_user,
            "remote": self.remote_user,
            "extension": self.extension,
            "state": self.state.value,
            "end_reason": self.end_reason,
            "final_status": self.final_status,
            "codec": self.codec,
            "auth_rounds": self.auth_rounds,
            "invite_to_100_ms": self.invite_to_100_ms,
            "invite_to_180_ms": self.invite_to_180_ms,
            "invite_to_200_ms": self.invite_to_200_ms,
            "established_to_first_rtp_tx_ms": self.established_to_first_rtp_tx_ms,
            "established_to_first_rtp_rx_ms": self.established_to_first_rtp_rx_ms,
            "duration_s": self.duration_s,
        }
        if self.media is not None:
            d["media"] = self.media.as_dict()
        return d
