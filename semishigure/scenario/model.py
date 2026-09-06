"""Scenario model (design §3.3) and YAML loader.

Only the parts needed by stage 1 are interpreted (caller / answerer / load
basics); unknown keys are kept in ``raw`` so later stages can grow the model
without breaking existing files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(ms|s|m)\s*$")


def parse_duration(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid duration: {value!r}")
    num, unit = float(m.group(1)), m.group(2) or "s"
    return num * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def parse_rate(value: Any, default: float = 1.0) -> float:
    """Calls per second."""
    if value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    m = _RATE_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid rate: {value!r}")
    num, unit = float(m.group(1)), m.group(2)
    return num / {"ms": 0.001, "s": 1, "m": 60}[unit]


@dataclass
class CallerConfig:
    auth_user: str
    auth_password_ref: str
    from_number: str
    destination: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    codec: str = "PCMU"
    audio: str | None = None
    audio_loop: bool = True
    steps: list[dict] = field(default_factory=list)
    from_display: str | None = None
    correlation_header: str | None = "X-Semishigure-Call"  # "" disables

    @property
    def codecs(self) -> list[str]:
        return [self.codec] + [c for c in ("PCMU", "PCMA") if c != self.codec]


@dataclass
class ExtensionConfig:
    user: str
    password_ref: str
    max_calls: int = 5


@dataclass
class AnswererConfig:
    extensions: list[ExtensionConfig]
    answer_after: float = 0.0
    audio: str | None = None
    audio_loop: bool = True
    ring_window: float = 0.05  # simultaneous ring: wait for sibling INVITEs before choosing who answers
    loser_grace: float = 1.0  # non-chosen legs answer anyway if the PBX has not CANCELled by then


@dataclass
class LoadConfig:
    target_concurrency: int = 1
    ramp_rate: float = 0.5
    call_duration: float = 180.0
    max_total_calls: int | None = None
    presets: dict[str, dict] = field(default_factory=dict)


@dataclass
class PbxTarget:
    host: str
    sip_port: int = 5060
    domain: str = ""
    local_ip: str | None = None
    caller_port: int = 5070
    answerer_port: int = 5080
    rtp_port_start: int = 20000
    rtp_port_end: int = 20999
    environment: str = "dev"
    transport: str = "udp"  # udp | tcp | tls
    tls_verify: bool = True  # verify the PBX certificate (false for self-signed dev PBXs)
    tls_ca: str | None = None  # CA bundle for verification
    tls_cert: str | None = None  # our listener certificate (default: self-signed, generated)
    tls_key: str | None = None


@dataclass
class Scenario:
    name: str
    caller: CallerConfig
    answerer: AnswererConfig
    load: LoadConfig
    pbx: PbxTarget
    description: str = ""
    pbx_profile: str = ""
    plugins: dict[str, Any] = field(default_factory=dict)
    monitor: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve_path(self, p: str | None) -> Path | None:
        if not p:
            return None
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)


def _headers(value: Any) -> list[tuple[str, str]]:
    if not value:
        return []
    if isinstance(value, dict):
        return [(str(k), _header_value(v)) for k, v in value.items()]
    out: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            for k, v in item.items():
                out.append((str(k), _header_value(v)))
        elif isinstance(item, list | tuple) and len(item) == 2:
            out.append((str(item[0]), _header_value(item[1])))
    return out


def _header_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return scenario_from_dict(data, base_dir=path.parent)


def scenario_from_dict(data: dict, base_dir: Path | None = None) -> Scenario:
    c = data.get("caller") or {}
    a = data.get("answerer") or {}
    ld = data.get("load") or {}
    p = data.get("pbx") or {}
    caller = CallerConfig(
        auth_user=str(c.get("auth_user", "")),
        auth_password_ref=str(c.get("auth_password_ref", "")),
        from_number=str(c.get("from_number", c.get("auth_user", ""))),
        destination=str(c.get("destination", "")),
        headers=_headers(c.get("headers")),
        codec=str(c.get("codec", "PCMU")).upper(),
        audio=c.get("audio"),
        audio_loop=bool(c.get("audio_loop", True)),
        steps=list(c.get("steps") or []),
        from_display=c.get("from_display"),
        correlation_header=(c.get("correlation_header", "X-Semishigure-Call") or None),
    )
    exts = [
        ExtensionConfig(user=str(e["user"]), password_ref=str(e.get("password_ref", "")), max_calls=int(e.get("max_calls", 5)))
        for e in (a.get("extensions") or [])
    ]
    answerer = AnswererConfig(
        extensions=exts,
        answer_after=parse_duration(a.get("answer_after"), 0.0),
        audio=a.get("audio"),
        audio_loop=bool(a.get("audio_loop", True)),
        ring_window=parse_duration(a.get("ring_window"), 0.05),
        loser_grace=parse_duration(a.get("loser_grace"), 1.0),
    )
    load = LoadConfig(
        target_concurrency=int(ld.get("target_concurrency", 1)) if not isinstance(ld.get("target_concurrency"), list) else int(ld["target_concurrency"][0]),
        ramp_rate=parse_rate(ld.get("ramp_rate"), 0.5),
        call_duration=parse_duration(ld.get("call_duration"), 180.0),
        max_total_calls=ld.get("max_total_calls"),
        presets=dict(ld.get("presets") or {}),
    )
    pbx = PbxTarget(
        host=str(p.get("host", "127.0.0.1")),
        sip_port=int(p.get("sip_port", 5060)),
        domain=str(p.get("domain", "")),
        local_ip=p.get("local_ip"),
        caller_port=int(p.get("caller_port", 5070)),
        answerer_port=int(p.get("answerer_port", 5080)),
        rtp_port_start=int(p.get("rtp_port_start", 20000)),
        rtp_port_end=int(p.get("rtp_port_end", 20999)),
        environment=str(p.get("environment", "dev")),
        transport=str(p.get("transport", "udp")).lower(),
        tls_verify=bool(p.get("tls_verify", True)),
        tls_ca=p.get("tls_ca"),
        tls_cert=p.get("tls_cert"),
        tls_key=p.get("tls_key"),
    )
    return Scenario(
        name=str(data.get("name", "scenario")),
        description=str(data.get("description", "")),
        pbx_profile=str(data.get("pbx_profile", "")),
        caller=caller,
        answerer=answerer,
        load=load,
        pbx=pbx,
        plugins=dict(data.get("plugins") or {}),
        monitor=dict(data.get("monitor") or {}),
        raw=data,
        base_dir=base_dir or Path.cwd(),
    )
