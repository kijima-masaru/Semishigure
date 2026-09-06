"""Temporarily change configuration files on the PBX host for the run and restore them afterwards.

Config::

    conf_override:
      files:
        - path: /etc/freeswitch/autoload_configs/flatline_interpreter.conf.xml
          params:                          # <param name="X" value="..."/> style (FreeSWITCH XML)
            langid-min-chars: 100000
          replace:                         # generic regex replacements (python re.sub)
            - { regex: "^loglevel=.*$", with: "loglevel=info" }
      reload: 'fs_cli -x reloadxml'        # optional command after apply and after restore
      require_confirmation: false          # refuse to run on prod unless the profile allows

A backup (``<path>.semishigure.bak``) is written before the change; post_run
(also on abnormal termination) restores it and runs ``reload`` again.
"""

from __future__ import annotations

import re
import shlex

from semishigure.plugins.base import Plugin, PluginContext


class ConfOverridePlugin(Plugin):
    """Applies temporary config changes on the PBX host and restores them after the run."""

    name = "conf_override"

    def __init__(self, config: dict, ctx: PluginContext):
        super().__init__(config, ctx)
        self.files = list(config.get("files") or [])
        self.reload_cmd = config.get("reload")
        self.applied: list[str] = []
        self.restored: list[str] = []

    def _executor(self):
        if self.ctx.adapter is None:
            raise RuntimeError("conf_override needs a PBX profile (executor)")
        return self.ctx.adapter.executor

    async def pre_run(self) -> None:
        ex = self._executor()
        for f in self.files:
            path = f["path"]
            r = await ex.run(f"cat {shlex.quote(path)}", timeout=10)
            if not r.ok:
                raise RuntimeError(f"conf_override: cannot read {path}: {r.stderr.strip()}")
            original = r.stdout
            new = apply_changes(original, f.get("params") or {}, f.get("replace") or [])
            if new == original:
                self.fail(f"{path}: no change applied (pattern not found?)")
                continue
            bak = path + ".semishigure.bak"
            r = await ex.run(f"cp -p {shlex.quote(path)} {shlex.quote(bak)}", timeout=10)
            if not r.ok:
                raise RuntimeError(f"conf_override: cannot write backup {bak}: {r.stderr.strip()}")
            await self._write(ex, path, new)
            self.applied.append(path)
            self.counters["applied"] += 1
        if self.applied and self.reload_cmd:
            await ex.run(self.reload_cmd, timeout=30)

    async def post_run(self) -> None:
        if not self.applied:
            return
        ex = self._executor()
        for path in list(self.applied):
            bak = path + ".semishigure.bak"
            r = await ex.run(f"mv -f {shlex.quote(bak)} {shlex.quote(path)}", timeout=10)
            if r.ok:
                self.restored.append(path)
                self.applied.remove(path)
                self.counters["restored"] += 1
            else:
                self.fail(f"restore failed for {path}: {r.stderr.strip()} (backup left at {bak})")
        if self.reload_cmd:
            await ex.run(self.reload_cmd, timeout=30)

    @staticmethod
    async def _write(ex, path: str, content: str) -> None:
        # heredoc with a quoted delimiter: no shell expansion of the content
        marker = "SEMISHIGURE_EOF_7f3a"
        r = await ex.run(f"cat > {shlex.quote(path)} <<'{marker}'\n{content}\n{marker}\n", timeout=10)
        if not r.ok:
            raise RuntimeError(f"conf_override: cannot write {path}: {r.stderr.strip()}")

    def snapshot(self) -> dict:
        return {"applied": list(self.applied), "restored": list(self.restored), "files": [f["path"] for f in self.files], "errors": self.errors[-5:]}

    def report_rows(self) -> list[tuple[str, object]]:
        desc = []
        for f in self.files:
            for k, v in (f.get("params") or {}).items():
                desc.append(f"{k}={v}")
            for r in f.get("replace") or []:
                desc.append(f"s/{r['regex']}/{r['with']}/")
        return [(f"{self.name}.conf の一時変更", ", ".join(desc) or "-"), (f"{self.name}.復元", "ok" if not self.applied else "未復元: " + ", ".join(self.applied))]


def apply_changes(text: str, params: dict, replacements: list[dict]) -> str:
    out = text
    for name, value in params.items():
        # <param name="X" value="old"/>  (any attribute order, any quoting)
        rx = re.compile(r'(<param\s+name\s*=\s*["\']' + re.escape(str(name)) + r'["\']\s+value\s*=\s*["\'])([^"\']*)(["\'])')
        out, n = rx.subn(lambda m, v=value: f"{m.group(1)}{v}{m.group(3)}", out)
        if n == 0:
            rx2 = re.compile(r'(<param\s+value\s*=\s*["\'])([^"\']*)(["\']\s+name\s*=\s*["\']' + re.escape(str(name)) + r'["\'])')
            out = rx2.sub(lambda m, v=value: f"{m.group(1)}{v}{m.group(3)}", out)
    for r in replacements:
        out = re.sub(r["regex"], str(r["with"]), out, flags=re.MULTILINE)
    return out
