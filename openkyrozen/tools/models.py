from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class CommandResult:
    """The durable outcome of one shell command, separate from its display text."""

    output: str
    success: bool
    exit_code: int | None = None
    failure: str | None = None

    def __str__(self) -> str:
        return self.output
