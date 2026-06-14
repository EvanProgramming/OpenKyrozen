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
    """Stores and retrieves interaction logs and source‑code snapshots.
    Uses two ChromaDB collections:
      - ``agent_logs``   : learning artifacts (facts, skills, strategies, conversations)
      - ``agent_files``  : source‑code snapshots (FILE: entries)
    This separation keeps FILE entries from dominating the embedding space and
    polluting semantic recall. Falls back to simple in‑memory lists when ChromaDB
    is not installed.
    """

    COLLECTION_NAME = "agent_logs"
    FILES_COLLECTION_NAME = "agent_files"
    DEFAULT_PATH = "./chroma_memory"

    def __init__(self, path: str | None = None):
        self._lock = threading.Lock()  # protect ChromaDB SQLite from concurrent access
        self._path = path or self.DEFAULT_PATH
        self._in_memory: list[tuple[str, str, str]] = []       # (id, text, timestamp)
        self._in_memory_files: list[tuple[str, str, str]] = []  # (id, text, timestamp)
        self._collection = None
        self._files_collection = None
        if _CHROMADB_AVAILABLE:
            try:
                self._client = chromadb.PersistentClient(
                    path=self._path,
                    settings=Settings(anonymized_telemetry=False),
                )
                self._collection = self._client.get_or_create_collection(
                    name=self.COLLECTION_NAME,
                    metadata={"description": "Agent learning artifacts and interaction logs"},
                )
                self._files_collection = self._client.get_or_create_collection(
                    name=self.FILES_COLLECTION_NAME,
                    metadata={"description": "Source‑code snapshots (FILE: entries)"},
                )
            except Exception as _e:
                print(f"[Memory] ChromaDB init error: {_e} – falling back to in‑memory storage.")
                self._client = None
                self._collection = None
                self._files_collection = None

    def add_log(self, text: str) -> str:
        """Save a text log (learning artifact, conversation, etc.) to the main
        ``agent_logs`` collection. Returns the assigned ID."""
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

    def add_file(self, rel_path: str, content: str) -> str:
        """Save a source‑code snapshot to the separate ``agent_files`` collection.
        This keeps FILE entries from polluting the learning‑artifact embedding space.
        Returns the assigned ID."""
        log_id = f"file_{datetime.now(timezone.utc).isoformat()}Z_{uuid.uuid4().hex[:8]}"
        now_ts = datetime.now(timezone.utc).isoformat()
        text = f"FILE: {rel_path}\n```python\n{content}\n```"
        with self._lock:
            if _CHROMADB_AVAILABLE and self._files_collection is not None:
                try:
                    self._files_collection.add(
                        ids=[log_id],
                        documents=[text],
                        metadatas=[{"timestamp": now_ts, "rel_path": rel_path}],
                    )
                except Exception:
                    pass  # silently skip file storage errors
            else:
                self._in_memory_files.append((log_id, text, now_ts))
        return log_id

    def remove_stale_files(self, valid_paths: set[str]) -> int:
        """Delete file snapshots whose ``rel_path`` no longer exists on disk.
        Returns the number of deleted entries."""
        removed = 0
        with self._lock:
            if _CHROMADB_AVAILABLE and self._files_collection is not None:
                try:
                    total = self._files_collection.count()
                    if total == 0:
                        return 0
                    # Scan the files collection for entries whose rel_path is stale
                    result = self._files_collection.get(
                        limit=min(total, 2000), offset=0, include=["metadatas", "documents"]
                    )
                    ids_to_delete = []
                    for doc_id, meta in zip(result.get("ids", []), result.get("metadatas", [])):
                        rp = meta.get("rel_path", "")
                        if rp and rp not in valid_paths:
                            ids_to_delete.append(doc_id)
                    if ids_to_delete:
                        self._files_collection.delete(ids=ids_to_delete)
                        removed = len(ids_to_delete)
                except Exception:
                    pass
            else:
                # In‑memory: keep only files whose path is in valid_paths
                keep = []
                for item in self._in_memory_files:
                    # rel_path is embedded in the text: "FILE: {rel_path}\n..."
                    text = item[1]
                    rp = text.split("\n")[0].replace("FILE: ", "", 1)
                    if rp in valid_paths:
                        keep.append(item)
                    else:
                        removed += 1
                self._in_memory_files = keep
        return removed

    def recall(self, query: str, n_results: int = 2) -> list[str]:
        """Retrieve the top n_results most relevant logs for the query from the
        ``agent_logs`` collection (learning artifacts only — FILE entries live in a
        separate ``agent_files`` collection).

        A lightweight FILE‑prefix safety filter catches any legacy FILE entries that
        predate the two‑collection split."""
        if not query or not query.strip():
            return []
        with self._lock:
            if _CHROMADB_AVAILABLE and self._collection is not None:
                try:
                    count = self._collection.count()
                    if count == 0:
                        return []
                    result = self._collection.query(
                        query_texts=[query.strip()],
                        n_results=min(n_results, count),
                    )
                    docs = result.get("documents")
                    if docs and len(docs) > 0:
                        first = docs[0]
                        if first is None:
                            return []
                        all_docs = list(first) if isinstance(first, list) else [first]
                        # Safety filter for legacy FILE entries still in agent_logs
                        return [d for d in all_docs if not d.startswith("FILE:")][:n_results]
                    return []
                except Exception:
                    return []
            else:
                if not self._in_memory:
                    return []
                recent = self._in_memory[-n_results:]
                return [text for _, text, _ in recent if not text.startswith("FILE:")]

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
