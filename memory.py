"""
Long-term memory for the AI agent (ChromaDB if available, else in‑memory).
"""

import threading
import warnings
import uuid
from datetime import datetime, timezone

warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*datetime.utcnow.*")

try:
    import chromadb
    from chromadb.config import Settings
    _CHROMADB_AVAILABLE = True
except ImportError:
    _CHROMADB_AVAILABLE = False
    chromadb = None
    Settings = None


class MemoryBank:
    """Stores and retrieves interaction logs.
    Uses ChromaDB when the optional chromadb package is installed,
    otherwise falls back to a simple in‑memory list.
    """

    COLLECTION_NAME = "agent_logs"
    DEFAULT_PATH = "./chroma_memory"

    def __init__(self, path: str | None = None):
        self._lock = threading.Lock()  # protect ChromaDB SQLite from concurrent access
        self._path = path or self.DEFAULT_PATH
        self._in_memory: list[tuple[str, str, str]] = []  # (id, text, timestamp)
        self._collection = None
        if _CHROMADB_AVAILABLE:
            try:
                self._client = chromadb.PersistentClient(
                    path=self._path,
                    settings=Settings(anonymized_telemetry=False),
                )
                self._collection = self._client.get_or_create_collection(
                    name=self.COLLECTION_NAME,
                    metadata={"description": "Agent interaction logs"},
                )
            except Exception as _e:
                print(f"[Memory] ChromaDB init error: {_e} – falling back to in‑memory storage.")
                self._client = None
                self._collection = None

    def add_log(self, text: str) -> str:
        """Save a text log with a timestamp‑based ID. Returns the assigned ID."""
        log_id = f"{datetime.now(timezone.utc).isoformat()}Z_{uuid.uuid4().hex[:8]}"
        now_ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    self._collection.add(
                        ids=[log_id],
                        documents=[text],
                        metadatas=[{"timestamp": now_ts}],
                    )
                except Exception as e:
                    log_id = f"err_{uuid.uuid4().hex[:8]}"
                    safe_doc = "[add_log error] " + str(text) + " (error: " + str(e) + ")"
                    self._collection.add(
                        ids=[log_id],
                        documents=[safe_doc],
                        metadatas=[{"timestamp": now_ts}],
                    )
            else:
                self._in_memory.append((log_id, text, now_ts))
        return log_id

    def recall(self, query: str, n_results: int = 2) -> list[str]:
        """Retrieve the top n_results most relevant non‑FILE logs for the query.
        FILE entries (source code copies) are excluded so semantic search returns
        actual learning artifacts (facts, skills, strategies, etc.)."""
        if not query or not query.strip():
            return []
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    count = self._collection.count()
                    # Fetch extra results to account for FILE entries being filtered out
                    fetch_n = min(max(n_results * 5, 10), count or 1)
                    result = self._collection.query(
                        query_texts=[query.strip()],
                        n_results=fetch_n,
                    )
                    docs = result.get("documents")
                    if docs and len(docs) > 0:
                        first = docs[0]
                        if first is None:
                            return []
                        all_docs = list(first) if isinstance(first, list) else [first]
                        # Exclude FILE entries — they are source code copies, not learning artifacts
                        filtered = [d for d in all_docs if not d.startswith("FILE:")]
                        return filtered[:n_results]
                    return []
                except Exception:
                    return []
            else:
                if not self._in_memory:
                    return []
                # In‑memory: take recent entries, filter out FILE entries
                candidates = self._in_memory[-max(n_results * 5, 10):]
                filtered = [text for _, text, _ in candidates if not text.startswith("FILE:")]
                return filtered[:n_results]

    def get_recent(self, n: int = 10) -> list[str]:
        """Return the n most recent logs (without relevance ranking)."""
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    total = self._collection.count()
                    if total == 0:
                        return []
                    n = min(n, total)
                    # ChromaDB get() returns in insertion order (oldest first).
                    # Offset to skip to the last n items, then reverse.
                    offset = total - n
                    result = self._collection.get(limit=n, offset=offset)
                    docs = result.get("documents", [])
                    docs.reverse()  # most recent first
                    return docs
                except Exception:
                    return []
            else:
                if not self._in_memory:
                    return []
                recent = self._in_memory[-n:]
                return [text for _, text, _ in recent]

    def count_logs(self) -> int:
        """Return the total number of stored logs."""
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    return self._collection.count()
                except Exception:
                    return 0
            else:
                return len(self._in_memory)

    def get_all(self, limit: int = 2000) -> tuple[list[str], list[str]]:
        """Return (ids, documents) for stored logs, up to ``limit``, most recent first. Thread-safe."""
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    total = self._collection.count()
                    if total == 0:
                        return ([], [])
                    n = min(total, limit)
                    # ChromaDB get() returns in insertion order (oldest first).
                    # Offset to skip to the last n items, then reverse.
                    offset = total - n
                    result = self._collection.get(limit=n, offset=offset,
                                                  include=["documents"])
                    ids = result.get("ids", [])
                    docs = result.get("documents", [])
                    ids.reverse()
                    docs.reverse()
                    return (ids, docs)
                except Exception:
                    return ([], [])
            else:
                ids = [item[0] for item in self._in_memory[-limit:]]
                docs = [item[1] for item in self._in_memory[-limit:]]
                return (ids, docs)

    def delete_logs(self, ids: list[str]) -> None:
        """Delete logs by ID. Thread-safe."""
        if not ids:
            return
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    self._collection.delete(ids=ids)
                except Exception:
                    pass
            else:
                self._in_memory = [item for item in self._in_memory if item[0] not in ids]
