"""Shared, failure-isolated plugin lifecycle runtime."""

from __future__ import annotations

import importlib.util
import re
import threading
from pathlib import Path
from typing import Any, Callable


_RUNTIMES: dict[str, "PluginRuntime"] = {}
_RUNTIMES_LOCK = threading.RLock()

_SECRET_PATTERNS = (
    re.compile(r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=])\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+"),
)


def _redact(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) + "<redacted>")
                           if match.lastindex else "<redacted>", text)
    return text.replace("\n", " ")[:limit]


class PluginRuntime:
    """Load plugins once for one execution surface and isolate every hook."""

    def __init__(self, surface: str, *, plugins_dir: Path | None = None,
                 event_recorder: Callable[[str, dict[str, Any]], None] | None = None):
        self.surface = str(surface or "unknown")
        self.plugins_dir = (plugins_dir or Path(__file__).parent / "plugins").resolve()
        self.event_recorder = event_recorder
        self._loaded = False
        self._plugins: list[tuple[str, Any]] = []
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def plugin_names(self) -> tuple[str, ...]:
        return tuple(name for name, _plugin in self._plugins)

    def _record(self, event_type: str, **payload: Any) -> None:
        if not self.event_recorder:
            return
        safe_payload = {key: _redact(value, 300) if isinstance(value, str) else value
                        for key, value in payload.items()}
        try:
            self.event_recorder(event_type, safe_payload)
        except Exception:
            pass

    def load_once(self) -> tuple[str, ...]:
        with self._lock:
            if self._loaded:
                return self.plugin_names
            # Set the flag before importing anything: a broken plugin must not
            # cause another request to retry imports or duplicate registrations.
            self._loaded = True
            if not self.plugins_dir.is_dir():
                self._record("plugin.loaded", plugin="<none>", status="no plugin directory")
                return self.plugin_names
            for path in sorted(self.plugins_dir.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                module_name = f"openkyrozen_plugin_{self.surface}_{path.stem}"
                try:
                    spec = importlib.util.spec_from_file_location(module_name, path)
                    if spec is None or spec.loader is None:
                        raise ImportError("could not create module spec")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    register = getattr(module, "register", None)
                    if not callable(register):
                        self._record("plugin.load_failed", plugin=path.stem,
                                     error="register() is missing")
                        continue
                    plugin = register()
                    if plugin is None:
                        self._record("plugin.load_failed", plugin=path.stem,
                                     error="register() returned no plugin")
                        continue
                    self._plugins.append((path.stem, plugin))
                    self._record("plugin.loaded", plugin=path.stem, status="loaded")
                except Exception as exc:
                    self._record("plugin.load_failed", plugin=path.stem,
                                 error=f"{type(exc).__name__}: {exc}")
            return self.plugin_names

    def trigger(self, hook_name: str, **kwargs: Any) -> None:
        self.load_once()
        for plugin_name, plugin in tuple(self._plugins):
            hook = getattr(plugin, hook_name, None)
            if not callable(hook):
                continue
            try:
                hook(**kwargs)
            except Exception as exc:
                self._record(
                    "plugin.hook_failed", plugin=plugin_name, hook=hook_name,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def turn_start(self, *, user_input: str, **context: Any) -> None:
        self.trigger("on_turn_start", user_input=_redact(user_input, 1000),
                     surface=self.surface, **context)

    def turn_end(self, *, reply: str, success: bool, error: str | None = None,
                 **context: Any) -> None:
        self.trigger("on_turn_end", reply=_redact(reply, 1000), success=bool(success),
                     error=_redact(error, 300) if error else None,
                     surface=self.surface, **context)

    def tool_execute(self, *, action: str, args: Any, result: Any, success: bool,
                     error: str | None = None, **context: Any) -> None:
        self.trigger(
            "on_tool_execute", action=_redact(action, 100), args=_redact(args, 500),
            result=_redact(result, 1000), success=bool(success),
            error=_redact(error, 300) if error else None,
            surface=self.surface, **context,
        )


def get_plugin_runtime(surface: str, *, plugins_dir: Path | None = None,
                       event_recorder: Callable[[str, dict[str, Any]], None] | None = None) -> PluginRuntime:
    """Return the one runtime instance for a surface in this process."""
    key = str(surface or "unknown")
    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = PluginRuntime(key, plugins_dir=plugins_dir, event_recorder=event_recorder)
            _RUNTIMES[key] = runtime
        return runtime
