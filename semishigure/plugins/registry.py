"""Plugin discovery and instantiation.

Scenario ``plugins:`` section::

    plugins:
      log_patterns:            # built-in plugin name
        counters: [...]
      my_service:              # any name; class from an importable module
        module: mypkg.semishigure_plugin:MyPlugin
        option: value
      flatline:
        enabled: false         # keep the config, skip the plugin
"""

from __future__ import annotations

import importlib
import logging

from semishigure.plugins.base import Plugin, PluginContext

log = logging.getLogger(__name__)

BUILTIN: dict[str, str] = {
    "log_patterns": "semishigure.plugins.log_patterns:LogPatternsPlugin",
    "status_command": "semishigure.plugins.status_command:StatusCommandPlugin",
    "conf_override": "semishigure.plugins.conf_override:ConfOverridePlugin",
    "ws_hook": "semishigure.plugins.ws_hook:WsHookPlugin",
    "webhook": "semishigure.plugins.webhook:WebhookPlugin",
}


def resolve_class(spec: str) -> type[Plugin]:
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise ValueError(f"plugin spec must be 'module:Class', got {spec!r}")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{spec} is not a semishigure Plugin subclass")
    return cls


def load_plugins(config: dict, ctx: PluginContext) -> list[Plugin]:
    plugins: list[Plugin] = []
    for name, cfg in (config or {}).items():
        cfg = dict(cfg or {})
        if cfg.pop("enabled", True) is False:
            continue
        spec = cfg.pop("module", None) or cfg.pop("plugin", None) or BUILTIN.get(name)
        if spec is None:
            log.warning("unknown plugin %r (built-ins: %s); ignored", name, ", ".join(BUILTIN))
            continue
        try:
            cls = resolve_class(spec)
            plugin = cls(cfg, ctx)
            plugin.name = name
            plugins.append(plugin)
        except Exception as exc:  # noqa: BLE001
            log.error("plugin %r could not be loaded: %s", name, exc)
            raise
    return plugins


def describe_builtin() -> list[dict]:
    out = []
    for name, spec in BUILTIN.items():
        try:
            cls = resolve_class(spec)
            out.append({"name": name, "class": spec, "description": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""})
        except Exception as exc:  # noqa: BLE001
            out.append({"name": name, "class": spec, "error": str(exc)})
    return out
