"""
Long-term memory for the AI agent using ChromaDB (persistent).
"""

import uuid
from datetime import datetime

import chromadb
from chromadb.config import Settings


class MemoryBank:
    """Stores and retrieves interaction logs using ChromaDB."""

    COLLECTION_NAME = "agent_logs"
    DEFAULT_PATH = "./chroma_memory"

    def __init__(self, path: str | None = None):
        self._path = path or self.DEFAULT_PATH
        self._client = chromadb.PersistentClient(
            path=self._path,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "Agent interaction logs"},
        )

    def add_log(self, text: str) -> str:
        """Save a text log with a timestamp-based ID. Returns the assigned ID."""
        log_id = f"{datetime.utcnow().isoformat()}Z_{uuid.uuid4().hex[:8]}"
        try:
            self._collection.add(
                ids=[log_id],
                documents=[text],
                metadatas=[{"timestamp": datetime.utcnow().isoformat()}],
            )
        except Exception as e:
            log_id = f"err_{uuid.uuid4().hex[:8]}"
            self._collection.add(
                ids=[log_id],
                documents=[f"[add_log error] {text} (error: {e})"],
                metadatas=[{"timestamp": datetime.utcnow().isoformat()}],
            )
        return log_id

    def recall(self, query: str, n_results: int = 2) -> list[str]:
        """Retrieve the top n_results most relevant logs for the query."""
        if not query or not query.strip():
            return []
        try:
            result = self._collection.query(
                query_texts=[query.strip()],
                n_results=min(n_results, self._collection.count() or 1),
            )
            docs = result.get("documents")
            if docs and len(docs) > 0:
                return list(docs[0]) if isinstance(docs[0], list) else [docs[0]]
            return []
        except Exception:
            return []
