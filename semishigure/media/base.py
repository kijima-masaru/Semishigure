"""Media boundary: the SIP layer only ever sees these interfaces.

A MediaEngine allocates sessions (one per call leg). A session owns an RTP
socket, sends a pre-encoded audio source at 20 ms pace and counts what it
receives. Replacing the Python implementation with a Go/Rust process only
requires another MediaEngine implementation; nothing in semishigure.sip or
semishigure.core depends on how packets are actually pumped.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from semishigure.media.wav import AudioSource


@dataclass
class MediaStats:
    codec: str | None = None
    tx_packets: int = 0
    rx_packets: int = 0
    rx_bytes: int = 0
    rx_lost: int = 0
    rx_ssrc: int | None = None
    first_tx_at: float | None = None  # monotonic
    first_rx_at: float | None = None
    last_rx_at: float | None = None
    # send-timing: deviation from the 20 ms schedule, seconds
    tx_late_max: float = 0.0
    tx_late_sum: float = 0.0
    tx_late_over_5ms: int = 0
    tx_late_over_20ms: int = 0
    rx_level_rms: float = 0.0  # recent RMS of decoded received audio (0..32767)
    rx_level_peak_rms: float = 0.0
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def tx_late_mean(self) -> float:
        return self.tx_late_sum / self.tx_packets if self.tx_packets else 0.0

    def as_dict(self) -> dict:
        return {
            "codec": self.codec,
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "rx_lost": self.rx_lost,
            "tx_late_max_ms": round(self.tx_late_max * 1000, 2),
            "tx_late_mean_ms": round(self.tx_late_mean * 1000, 3),
            "tx_late_over_5ms": self.tx_late_over_5ms,
            "tx_late_over_20ms": self.tx_late_over_20ms,
            "rx_level_rms": round(self.rx_level_rms, 1),
            "rx_level_peak_rms": round(self.rx_level_peak_rms, 1),
        }


class MediaSession(ABC):
    """One RTP endpoint for one call leg."""

    local_ip: str
    local_port: int

    @abstractmethod
    def set_remote(self, ip: str, port: int, codec: str, payload_type: int) -> None:
        """Peer address and negotiated codec (from the SDP answer/offer)."""

    @abstractmethod
    def start(self) -> None:
        """Begin sending the audio source (if any). Receiving starts at creation."""

    @abstractmethod
    def stop(self) -> None:
        """Stop sending/receiving and release the port."""

    @abstractmethod
    def stats(self) -> MediaStats: ...


class MediaEngine(ABC):
    @abstractmethod
    def create_session(self, local_ip: str, source: AudioSource | None, record_rx: Path | None = None) -> MediaSession: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def describe(self) -> dict: ...
