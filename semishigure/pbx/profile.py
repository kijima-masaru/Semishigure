"""PBX profiles (design §3.1): where the PBX is and how to reach it.

Profiles live in ``$SEMISHIGURE_HOME/pbx_profiles.yaml`` (default
``~/.semishigure``). Secrets are references only (``secret:NAME``)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from semishigure.secrets import DEFAULT_DIR, SecretStore


@dataclass
class PbxProfile:
    name: str
    type: str = "freeswitch"  # freeswitch | asterisk
    host: str = "127.0.0.1"  # SIP signalling address
    sip_port: int = 5060
    domain: str = ""
    rtp_port_start: int = 16384
    rtp_port_end: int = 32768
    environment: str = "dev"  # dev | staging | prod
    max_concurrency: int | None = None  # profile-level cap (enforced for prod)
    executor: str = "local"  # local | ssh
    ssh_host: str = ""  # defaults to host
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key: str = ""  # path to the private key
    ssh_key_passphrase_ref: str = ""  # secret:NAME
    ssh_known_hosts: str = ""  # path; empty = ~/.ssh/known_hosts
    ssh_strict_host_key: bool = True
    esl_host: str = "127.0.0.1"  # as seen from the PBX host (port-forwarded over SSH)
    esl_port: int = 8021
    esl_password_ref: str = ""
    fs_cli: str = "fs_cli"
    log_path: str = "/var/log/freeswitch/freeswitch.log"
    process_name: str = "freeswitch"
    conf_dir: str = "/etc/freeswitch"
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_prod(self) -> bool:
        return self.environment.lower() == "prod"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PbxProfile:
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known and k != "extra"}
        extra = dict(data.get("extra") or {})
        extra.update({k: v for k, v in data.items() if k not in known})
        return cls(**kwargs, extra=extra)

    def public(self) -> dict:
        """Safe for the UI (references only, never secrets)."""
        return self.to_dict()


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (DEFAULT_DIR / "pbx_profiles.yaml")

    def load_all(self) -> list[PbxProfile]:
        if not self.path.exists():
            return []
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        items = data.get("profiles", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = [{"name": k, **(v or {})} for k, v in items.items()]
        return [PbxProfile.from_dict(d) for d in items or []]

    def save_all(self, profiles: list[PbxProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump({"profiles": [p.to_dict() for p in profiles]}, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def get(self, name: str) -> PbxProfile | None:
        for p in self.load_all():
            if p.name == name:
                return p
        return None

    def upsert(self, profile: PbxProfile) -> None:
        profiles = [p for p in self.load_all() if p.name != profile.name]
        profiles.append(profile)
        self.save_all(profiles)

    def delete(self, name: str) -> bool:
        profiles = self.load_all()
        kept = [p for p in profiles if p.name != name]
        if len(kept) == len(profiles):
            return False
        self.save_all(kept)
        return True


def build_executor(profile: PbxProfile, secrets: SecretStore | None = None):
    from semishigure.pbx.executor import LocalExecutor, SshExecutor

    if profile.executor == "local":
        return LocalExecutor()
    if profile.executor == "ssh":
        passphrase = None
        if profile.ssh_key_passphrase_ref:
            passphrase = (secrets or SecretStore()).resolve(profile.ssh_key_passphrase_ref)
        return SshExecutor(
            host=profile.ssh_host or profile.host,
            username=profile.ssh_user,
            port=profile.ssh_port,
            key_path=profile.ssh_key or None,
            passphrase=passphrase,
            known_hosts=profile.ssh_known_hosts or None,
            strict_host_key=profile.ssh_strict_host_key,
        )
    raise ValueError(f"unknown executor {profile.executor!r}")
