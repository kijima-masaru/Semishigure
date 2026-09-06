"""PluginManager: fans out hooks to loaded plugins and never lets one plugin break the run."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from semishigure.plugins.base import Plugin, PluginContext
from semishigure.plugins.registry import load_plugins

log = logging.getLogger(__name__)


class PluginManager:
    def __init__(self, config: dict, ctx: PluginContext):
        self.ctx = ctx
        self.plugins: list[Plugin] = load_plugins(config, ctx)
        self._post_run_done = False

    def __len__(self) -> int:
        return len(self.plugins)

    async def pre_run(self) -> None:
        for p in self.plugins:
            await p.pre_run()  # errors abort the run on purpose

    async def post_run(self) -> None:
        if self._post_run_done:
            return
        self._post_run_done = True
        for p in self.plugins:
            try:
                await asyncio.wait_for(p.post_run(), 30)
            except Exception as exc:  # noqa: BLE001
                p.fail(f"post_run: {exc}")

    def call_established(self, call) -> None:
        for p in self.plugins:
            self._spawn(p, p.on_call_established(call), "on_call_established")

    def call_ended(self, call) -> None:
        for p in self.plugins:
            self._spawn(p, p.on_call_ended(call), "on_call_ended")

    def _spawn(self, p: Plugin, coro, hook: str) -> None:
        async def runner():
            try:
                await coro
            except Exception as exc:  # noqa: BLE001
                p.fail(f"{hook}: {exc}")

        try:
            asyncio.get_running_loop().create_task(runner())
        except RuntimeError:
            coro.close()

    async def collect_metrics(self, adapter) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in self.plugins:
            try:
                m = await asyncio.wait_for(p.collect_metrics(adapter), 20)
            except Exception as exc:  # noqa: BLE001
                p.fail(f"collect_metrics: {exc}")
                continue
            for k, v in (m or {}).items():
                out[f"{p.name}.{k}"] = v
        return out

    def parse_log_line(self, line: str) -> None:
        for p in self.plugins:
            try:
                p.parse_log_line(line)
            except Exception as exc:  # noqa: BLE001
                p.fail(f"parse_log_line: {exc}")

    def snapshot(self) -> dict[str, Any]:
        return {p.name: {"class": p.__class__.__name__, **p.snapshot()} for p in self.plugins}

    def report_rows(self) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        for p in self.plugins:
            try:
                rows.extend(p.report_rows())
            except Exception as exc:  # noqa: BLE001
                p.fail(f"report_rows: {exc}")
        return rows
