"""Secret store: YAML/config files only carry references such as ``secret:9100``.

Resolution order for ``secret:NAME``:
1. environment variable ``SEMISHIGURE_SECRET_<NAME>`` (NAME upper-cased, ``-``/``.`` -> ``_``)
2. encrypted file store ``~/.semishigure/secrets.enc`` (Fernet; key from
   ``SEMISHIGURE_MASTER_KEY`` or ``~/.semishigure/master.key``)

A bare value without the ``secret:`` prefix is rejected so that passwords
never end up in scenario files by accident (use ``literal:`` explicitly for
throw-away development setups).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("SEMISHIGURE_HOME", Path.home() / ".semishigure"))


class SecretError(RuntimeError):
    pass


def _env_name(name: str) -> str:
    return "SEMISHIGURE_SECRET_" + "".join(ch if ch.isalnum() else "_" for ch in name).upper()


class SecretStore:
    def __init__(self, home: Path | None = None):
        self.home = home or DEFAULT_DIR
        self.file = self.home / "secrets.enc"
        self.key_file = self.home / "master.key"
        self._cache: dict[str, str] | None = None

    # -- key management ---------------------------------------------------------

    def _fernet(self, create: bool = False):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover
            raise SecretError("the 'cryptography' package is required for the encrypted secret store") from exc
        key = os.environ.get("SEMISHIGURE_MASTER_KEY")
        if not key:
            if self.key_file.exists():
                key = self.key_file.read_text().strip()
            elif create:
                key = Fernet.generate_key().decode()
                self.home.mkdir(parents=True, exist_ok=True)
                self.key_file.write_text(key)
                try:
                    os.chmod(self.key_file, 0o600)
                except OSError:
                    pass
            else:
                raise SecretError(f"no master key: set SEMISHIGURE_MASTER_KEY or create {self.key_file}")
        return Fernet(key.encode() if isinstance(key, str) else key)

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self.file.exists():
            self._cache = {}
            return self._cache
        f = self._fernet()
        data = f.decrypt(self.file.read_bytes())
        self._cache = json.loads(data.decode())
        return self._cache

    def _save(self, values: dict[str, str]) -> None:
        f = self._fernet(create=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(f.encrypt(json.dumps(values).encode()))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass
        self._cache = values

    # -- public API ---------------------------------------------------------------

    def set(self, name: str, value: str) -> None:
        values = dict(self._load()) if self.file.exists() else {}
        values[name] = value
        self._save(values)

    def delete(self, name: str) -> bool:
        values = dict(self._load())
        if name not in values:
            return False
        del values[name]
        self._save(values)
        return True

    def names(self) -> list[str]:
        return sorted(self._load().keys()) if self.file.exists() else []

    def resolve(self, ref: str) -> str:
        """Resolve ``secret:NAME`` / ``env:VAR`` / ``literal:VALUE``."""
        if ref.startswith("literal:"):
            return ref[len("literal:") :]
        if ref.startswith("env:"):
            v = os.environ.get(ref[4:])
            if v is None:
                raise SecretError(f"environment variable {ref[4:]} is not set")
            return v
        if not ref.startswith("secret:"):
            raise SecretError(f"password/secret fields must be references (secret:NAME, env:VAR or literal:...), got {ref!r}")
        name = ref[len("secret:") :]
        v = os.environ.get(_env_name(name))
        if v is not None:
            return v
        try:
            values = self._load()
        except SecretError:
            values = {}
        if name in values:
            return values[name]
        raise SecretError(f"secret {name!r} not found: set {_env_name(name)} or run 'semishigure secret set {name}'")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
