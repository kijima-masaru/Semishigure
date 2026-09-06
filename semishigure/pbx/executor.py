"""Executor: run shell commands on the PBX host, locally or over SSH (design §3.1)."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()


class ExecutorError(RuntimeError):
    pass


class Executor(ABC):
    kind: str = "abstract"

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def run(self, command: str, timeout: float = 15.0) -> CommandResult: ...

    @abstractmethod
    def stream(self, command: str) -> AsyncIterator[str]:
        """Run a long-lived command (tail -F) and yield stdout lines."""

    @abstractmethod
    async def forward(self, host: str, port: int) -> tuple[str, int]:
        """Return a (host, port) reachable from this process that leads to host:port on the PBX host."""

    def describe(self) -> dict:
        return {"kind": self.kind}


def _posix_shell() -> str | None:
    """On Windows the commands (cat, ps, tail, heredocs) need a POSIX shell: Git for Windows
    or MSYS2 bash when installed. WSL's System32\\bash.exe is not used (different filesystem)."""
    if sys.platform != "win32":
        return None
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "usr", "bin", "bash.exe"),
        r"C:\msys64\usr\bin\bash.exe",
    ]
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        candidates.append(found)
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise ExecutorError("the local executor needs a POSIX shell on Windows (Git for Windows bash); use executor: ssh to reach the PBX host")


class LocalExecutor(Executor):
    kind = "local"

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @staticmethod
    async def _spawn(command: str, stderr):
        shell = _posix_shell()
        if shell:
            return await asyncio.create_subprocess_exec(shell, "-c", command, stdout=asyncio.subprocess.PIPE, stderr=stderr)
        return await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=stderr)

    async def run(self, command: str, timeout: float = 15.0) -> CommandResult:
        t0 = time.monotonic()
        proc = await self._spawn(command, asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            raise ExecutorError(f"timeout after {timeout}s: {command}") from None
        return CommandResult(command, proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), round((time.monotonic() - t0) * 1000, 1))

    async def stream(self, command: str) -> AsyncIterator[str]:
        proc = await self._spawn(command, asyncio.subprocess.STDOUT)
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.shield(asyncio.wait_for(proc.wait(), 2))
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()  # close the pipes now, not at interpreter exit

    async def forward(self, host: str, port: int) -> tuple[str, int]:
        return host, port


class SshExecutor(Executor):
    """asyncssh based executor. Key authentication only; the passphrase, if
    any, comes from the secret store (never from the profile file)."""

    kind = "ssh"

    def __init__(self, host: str, username: str, port: int = 22, key_path: str | Path | None = None, passphrase: str | None = None, known_hosts: str | Path | None = None, strict_host_key: bool = True, connect_timeout: float = 10.0):
        self.host = host
        self.port = port
        self.username = username
        self.key_path = Path(key_path).expanduser() if key_path else None
        self.passphrase = passphrase
        self.known_hosts = Path(known_hosts).expanduser() if known_hosts else None
        self.strict_host_key = strict_host_key
        self.connect_timeout = connect_timeout
        self._conn = None
        self._listeners: list = []

    async def connect(self) -> None:
        try:
            import asyncssh
        except ImportError as exc:  # pragma: no cover
            raise ExecutorError("asyncssh is required for SSH executors") from exc
        options: dict = {"username": self.username, "port": self.port}
        if self.key_path:
            options["client_keys"] = [str(self.key_path)]
        if self.passphrase:
            options["passphrase"] = self.passphrase
        if self.strict_host_key:
            options["known_hosts"] = str(self.known_hosts) if self.known_hosts else ()
        else:
            options["known_hosts"] = None
        try:
            self._conn = await asyncio.wait_for(asyncssh.connect(self.host, **options), self.connect_timeout)
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise ExecutorError(f"ssh {self.username}@{self.host}:{self.port}: {exc}") from exc
        log.info("ssh connected to %s@%s:%d", self.username, self.host, self.port)

    async def close(self) -> None:
        for listener in self._listeners:
            listener.close()
        self._listeners = []
        if self._conn is not None:
            self._conn.close()
            try:
                await asyncio.wait_for(self._conn.wait_closed(), 3)
            except (TimeoutError, Exception):  # noqa: BLE001
                pass
            self._conn = None

    def _require(self):
        if self._conn is None:
            raise ExecutorError("ssh executor not connected")
        return self._conn

    async def run(self, command: str, timeout: float = 15.0) -> CommandResult:
        conn = self._require()
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(conn.run(command, check=False), timeout)
        except TimeoutError:
            raise ExecutorError(f"timeout after {timeout}s: {command}") from None
        return CommandResult(command, r.exit_status or 0, str(r.stdout or ""), str(r.stderr or ""), round((time.monotonic() - t0) * 1000, 1))

    async def stream(self, command: str) -> AsyncIterator[str]:
        conn = self._require()
        proc = await conn.create_process(command, stderr=__import__("asyncssh").STDOUT)
        try:
            async for line in proc.stdout:
                yield str(line).rstrip("\r\n")
        finally:
            proc.kill()
            proc.close()

    async def forward(self, host: str, port: int) -> tuple[str, int]:
        conn = self._require()
        listener = await conn.forward_local_port("127.0.0.1", 0, host, port)
        self._listeners.append(listener)
        return "127.0.0.1", listener.get_port()

    def describe(self) -> dict:
        return {"kind": self.kind, "host": self.host, "port": self.port, "user": self.username, "key": str(self.key_path) if self.key_path else None}


def quote(arg: str) -> str:
    return shlex.quote(arg)
