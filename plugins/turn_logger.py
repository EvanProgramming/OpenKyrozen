"""
Example plugin: logs every chat turn to a file.
Copy this to create your own plugins.
"""

import time
import os
from pathlib import Path

class TurnLogger:
    """Logs every conversation turn with timestamp."""

    def __init__(self):
        self._log_path = Path(os.environ.get("KYROZEN_TURN_LOG", "kyrozen_turns.log"))

    def on_turn_start(self, user_input: str, **kwargs):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] USER: {user_input[:200]}\n")

    def on_turn_end(self, reply: str, **kwargs):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] AGENT: {reply[:200]}\n")

    def on_tool_execute(self, action: str, args: str, result: str, **kwargs):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] TOOL: {action}({args[:100]}) -> {result[:100]}\n")


def register():
    return TurnLogger()
