"""Optional Chroma adapter. SQLite stays authoritative if this index fails."""
from pathlib import Path

class ChromaIndex:
    def __init__(self, path):
        import chromadb
        from chromadb.config import Settings
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))
        self.memories = self.client.get_or_create_collection(
            name="agent_logs", metadata={"description": "Derived index for durable OpenKyrozen memories"})
        self.files = self.client.get_or_create_collection(
            name="agent_files", metadata={"description": "Derived index for workspace source files"})
