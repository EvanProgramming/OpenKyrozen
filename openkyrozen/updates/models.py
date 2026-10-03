"""Component outcomes for package-managed updates."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UpdateResult:
    status: str
    message: str
    revision: str | None = None
    components: dict[str, str] = field(default_factory=dict)
    restart_ready: bool = False

    def __str__(self) -> str:
        return self.message

INSTALL_PROBE = """import importlib.metadata as m, json, openkyrozen
from openkyrozen.interfaces.cli import launcher
from openkyrozen.interfaces.tui import backend
from openkyrozen.interfaces.web import app
d=m.distribution('openkyrozen')
print(json.dumps({'version':d.version,'source':json.loads(d.read_text('direct_url.json') or '{}'),
'paths':[module.__file__ for module in (openkyrozen,launcher,backend,app)]}))
"""
