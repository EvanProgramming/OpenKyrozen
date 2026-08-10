"""Versioned tool catalog used by runtime and sub-agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from capability_tokens import CapabilityToken


@dataclass(frozen=True)
class ToolManifest:
    name: str
    capability: str
    risk: str = "normal"
    version: str = "1.0.0"
    source: str = "builtin"


class ToolRegistry:
    def __init__(self, tools: dict[str, Callable[..., Any]] | None = None):
        self.tools = tools if tools is not None else {}
        self.manifests: dict[str, ToolManifest] = {}

    def register(self, name: str, function: Callable[..., Any], *, capability: str = "dynamic",
                 risk: str = "normal", version: str = "1.0.0", source: str = "builtin") -> None:
        self.tools[name] = function
        self.manifests[name] = ToolManifest(name, capability, risk, version, source)

    def catalog(self, token: CapabilityToken | None = None) -> list[dict[str, Any]]:
        result = []
        for name, function in sorted(self.tools.items()):
            manifest = self.manifests.get(name, ToolManifest(name, "dynamic"))
            if token is not None and not token.allows(manifest.capability):
                continue
            result.append(asdict(manifest) | {"description": (function.__doc__ or "").strip().split("\n")[0]})
        return result

    def invoke(self, name: str, args: Any, token: CapabilityToken) -> Any:
        function = self.tools.get(name)
        if function is None:
            raise KeyError(f"Unknown tool: {name}")
        manifest = self.manifests.get(name, ToolManifest(name, "dynamic"))
        if not token.allows(manifest.capability):
            raise PermissionError(f"Capability '{manifest.capability}' is not granted by token")
        return function(args)
