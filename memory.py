"""
Long-term memory for the AI agent (ChromaDB if available, else in‑memory).
"""

import uuid
from datetime import datetime

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
            self._client = chromadb.PersistentClient(
                path=self._path,
                settings=Settings(anonymized_telemetry=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"description": "Agent interaction logs"},
            )

    def add_log(self, text: str) -> str:
        """Save a text log with a timestamp‑based ID. Returns the assigned ID."""
        log_id = f"{datetime.utcnow().isoformat()}Z_{uuid.uuid4().hex[:8]}"
        now_ts = datetime.utcnow().isoformat()
        if _CHROMADB_AVAILABLE and self._collection is not None:
            try:
                self._collection.add(
                    ids=[log_id],
                    documents=[text],
                    metadatas=[{"timestamp": now_ts}],
                )
            except Exception as e:
                log_id = f"err_{uuid.uuid4().hex[:8]}"
                self._collection.add(
                    ids=[log_id],
                    documents=[f"[add_log error] {text} (error: {e})"],
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
                    return list(docs[0]) if isinstance(docs[0], list) else [docs[0]]
                return []
            except Exception:
                return []
        else:
            # Naive fallback: return the n_results most recent logs (no real search)
            if not self._in_memory:
                return []
            recent = self._in_memory[-n_results:]
            return [text for _, text, _ in recent]
