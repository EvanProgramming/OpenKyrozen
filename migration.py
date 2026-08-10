"""One-way, non-destructive migration helpers for OpenKyrozen v1 data."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory import MemoryBank


def migrate_v1_chroma(source: str | Path, target_db: str | Path, *, workspace_id: str = "default") -> dict[str, Any]:
    """Import v1 Chroma collections into v2 SQLite plus a rebuildable index."""
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target_db).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Legacy memory path not found: {source_path}")
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        raise RuntimeError("ChromaDB is required only to import v1 data; install chromadb and retry") from exc

    backup = source_path.with_name(source_path.name + ".v1-backup")
    if not backup.exists():
        shutil.copytree(source_path, backup)
    client = chromadb.PersistentClient(path=str(source_path), settings=Settings(anonymized_telemetry=False))
    memory = MemoryBank(target_path, workspace_id=workspace_id)
    imported_logs = 0
    imported_files = 0
    for name in ("agent_logs", "agent_files"):
        try:
            collection = client.get_collection(name)
        except Exception:
            continue
        data = collection.get(include=["documents", "metadatas"])
        documents = data.get("documents", [])
        metadatas = data.get("metadatas", [])
        for document, metadata in zip(documents, metadatas):
            if not document:
                continue
            metadata = metadata or {}
            if name == "agent_files" or str(document).startswith("FILE:"):
                rel_path = str(metadata.get("rel_path", ""))
                if not rel_path:
                    rel_path = str(document).split("\n", 1)[0].removeprefix("FILE: ")
                content = str(document).split("\n", 2)[-1]
                content = content.removeprefix("```python\n").removesuffix("\n```")
                memory.add_file(rel_path, content)
                imported_files += 1
            else:
                memory.add_log(str(document), kind=memory._kind_for_text(str(document)),
                               metadata={"migrated_from": str(source_path), "legacy_metadata": metadata})
                imported_logs += 1
    report = {
        "source": str(source_path), "backup": str(backup), "target": str(target_path),
        "workspace_id": workspace_id, "imported_logs": imported_logs,
        "imported_files": imported_files,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }
    memory.store.append_event("migration.v1_completed", report, workspace_id=workspace_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OpenKyrozen v1 Chroma memory into v2")
    parser.add_argument("source", help="Path to the v1 chroma_memory directory")
    parser.add_argument("--db", required=True, help="Target v2 SQLite database")
    parser.add_argument("--workspace", default="default")
    args = parser.parse_args()
    print(json.dumps(migrate_v1_chroma(args.source, args.db, workspace_id=args.workspace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
