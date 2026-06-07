"""
Long-term memory for the AI agent (ChromaDB if available, else in‑memory).
"""

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
        if _CHROMADB_AVAILABLE and self._collection is not None:
            try:
                self._collection.add(
                    ids=[log_id],
                    documents=[text],
                    metadatas=[{"timestamp": now_ts}],
                )
            except Exception as e:
                log_id = f"err_{uuid.uuid4().hex[:8]}"
                # Use safe concatenation to avoid f-string parsing of user data
                safe_doc = "[add_log error] " + str(text) + " (error: " + str(e) + ")"
                self._collection.add(
                    ids=[log_id],
                    documents=[safe_doc],
                    metadatas=[{"timestamp": now_ts}],
                )
        else:
            # In‑memory fallback: just append
            self._in_memory.append((log_id, text, now_ts))
        return log_id

    def recall(self, query: str, n_results: int = 2) -> list[str]:
        """Retrieve the top n_results most relevant logs for the query."""
        if not query or not query.strip():
            return []
        if _CHROMADB_AVAILABLE and self._collection is not None:
            try:
                count = self._collection.count()
                result = self._collection.query(
                    query_texts=[query.strip()],
                    n_results=min(n_results, count or 1),
                )
                docs = result.get("documents")
                if docs and len(docs) > 0:
                    first = docs[0]
                    if first is None:
                        return []
                    return list(first) if isinstance(first, list) else [first]
                return []
            except Exception:
                return []
        else:
            # Naive fallback: return the n_results most recent logs (no real search)
            if not self._in_memory:
                return []
            recent = self._in_memory[-n_results:]
            return [text for _, text, _ in recent]

    def get_recent(self, n: int = 10) -> list[str]:
        """Return the n most recent logs (without relevance ranking)."""
        if _CHROMADB_AVAILABLE and self._collection is not None:
            try:
                result = self._collection.get(limit=n, offset=0)
                docs = result.get("documents", [])
                return docs if docs else []
            except Exception:
                return []
        else:
            if not self._in_memory:
                return []
            recent = self._in_memory[-n:]
            return [text for _, text, _ in recent]

    def count_logs(self) -> int:
        """Return the total number of stored logs."""
        if _CHROMADB_AVAILABLE and self._collection is not None:
            try:
                return self._collection.count()
            except Exception:
                return 0
        else:
            return len(self._in_memory)
