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
