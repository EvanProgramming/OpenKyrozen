from __future__ import annotations

from typing import Any

def _store_failure(self, original_request: str, attempted_action: str, error_info: str, resolution: str) -> None:
    """Store a failure incident in long‑term memory for later retrieval."""
    entry = (
        f"{self._FAILURE_STORE_PREFIX}\n"
        f"Request: {original_request}\n"
        f"Attempted: {attempted_action}\n"
        f"Error: {error_info}\n"
        f"Resolution: {resolution}\n"
    )
    self.memory_bank.add_log(entry)


def _retrieve_failure(self, query: str, n: int = 3) -> list[str]:
    """Retrieve relevant failure records from memory."""
    results = self.memory_bank.recall(query, n_results=n)
    return [r for r in results if r.startswith(self._FAILURE_STORE_PREFIX)]


def _emit_stream_event(self, event: dict[str, Any]) -> None:
    """Forward typed stream progress without letting a client affect the turn."""
    callback = self._stream_event_callback.get()
    if not callable(callback):
        return
    try:
        callback(event)
    except Exception:
        pass


def _track_tool_performance(self, action: str, result: str, elapsed: float) -> None:
    stats = self._tool_stats.setdefault(action, {"calls":0,"successes":0,"total_time":0.0})
    stats["calls"] += 1
    stats["total_time"] += elapsed
    if not self._is_tool_error(result):
        stats["successes"] += 1
