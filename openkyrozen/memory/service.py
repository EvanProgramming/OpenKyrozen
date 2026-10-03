"""Versioned memory service.

SQLite is authoritative. ChromaDB, when installed, is only a derived semantic
index and can be deleted/rebuilt without losing memories.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import copy
from openkyrozen.persistence.models import stable_hash
from .ports import MemoryStore, VectorIndex


class MemoryBank:
    """Backward-compatible facade over the v2 durable memory service."""

    COLLECTION_NAME = "agent_logs"
    FILES_COLLECTION_NAME = "agent_files"
    DEFAULT_PATH = os.path.expanduser("~/.kyrozen/v2/openkyrozen.sqlite3")
    MAX_LOGS = int(os.environ.get("KYROZEN_MEMORY_MAX_LOGS", "10000"))

    def __init__(self, path: str | os.PathLike[str] | None = None, *, user_id: str = "local",
                 workspace_id: str = "default", session_id: str | None = None,
                 file_scope_id: str | None = None, store: MemoryStore, index: VectorIndex | None = None):
        self._lock = threading.RLock()
        self.user_id = user_id
        self.workspace_id = workspace_id
        # Conversation, task, and learning state may be global while source
        # snapshots remain isolated to the active project.
        self.file_scope_id = file_scope_id or workspace_id
        self.session_id = session_id
        self.db_path = Path(path or store.path)
        self.store = store
        self._collection = index.memories if index is not None else None
        self._files_collection = index.files if index is not None else None
        self._client = index

    def scoped(self, *, user_id=None, workspace_id=None, session_id=None, file_scope_id=None):
        """Share application storage/index while owning independent scope and locks."""
        view = copy.copy(self)
        view._lock = threading.RLock()
        view.user_id = self.user_id if user_id is None else user_id
        view.workspace_id = self.workspace_id if workspace_id is None else workspace_id
        view.session_id = session_id
        view.file_scope_id = self.file_scope_id if file_scope_id is None else file_scope_id
        return view

    @staticmethod
    def _kind_for_text(text: str) -> str:
        prefix = text.split(":", 1)[0].strip().upper()
        return {
            "FACT": "fact", "SKILL": "skill", "STRATEGY": "strategy", "PREF": "preference",
            "REFLECTION": "strategy", "FAILURE": "failure", "FIX_OUTCOME": "failure",
            "FILE": "source", "USER": "episodic", "GRAPH": "fact",
        }.get(prefix, "episodic")

    def _scope_kwargs(self) -> dict[str, str | None]:
        return {"user_id": self.user_id, "workspace_id": self.workspace_id, "session_id": self.session_id}

    def _index_memory(self, memory_id: str, text: str, *, kind: str, status: str = "active") -> None:
        if self._collection is None:
            return
        try:
            self._collection.upsert(
                ids=[memory_id], documents=[text],
                metadatas=[{"kind": kind, "status": status, "user_id": self.user_id,
                             "workspace_id": self.workspace_id,
                             "session_id": self.session_id or "", "memory_id": memory_id}],
            )
        except Exception as exc:
            self._last_error = f"memory index write failed: {exc}"

    def add_log(self, text: str, *, kind: str | None = None, status: str = "active",
                confidence: float = 0.5, source_event_ids: list[str] | None = None,
                metadata: dict[str, Any] | None = None) -> str:
        text = str(text)
        kind = kind or self._kind_for_text(text)
        event_content = "[private claim]" if (metadata or {}).get("visibility") == "private" else text
        event_id = self.store.append_event("memory.observed", {"kind": kind, "content": event_content},
                                           **self._scope_kwargs())
        memory_id = self.store.upsert_memory(
            text, kind=kind, status=status, confidence=confidence,
            source_event_ids=[event_id, *(source_event_ids or [])], metadata=metadata,
            **self._scope_kwargs(),
        )
        self._index_memory(memory_id, text, kind=kind, status=status)
        feature = (metadata or {}).get("learning_feature")
        if feature and status == "active":
            self.store.append_event("learning.product_created", {
                "feature": feature, "memory_id": memory_id,
            }, **self._scope_kwargs())
        self._trim_logs()
        return memory_id

    def add_candidate(self, text: str, *, kind: str = "fact", confidence: float = 0.0,
                      evidence: list[str] | None = None, metadata: dict[str, Any] | None = None) -> str:
        proposal_id = self.store.create_proposal(kind, text, confidence=confidence, evidence=evidence,
                                                  workspace_id=self.workspace_id, user_id=self.user_id)
        self.store.append_event("learning.candidate", {"proposal_id": proposal_id, "kind": kind}, **self._scope_kwargs())
        return proposal_id

    def promote_candidate(self, proposal_id: str, *, confidence: float = 0.8,
                          validation: dict[str, Any] | None = None) -> bool:
        proposals = [p for p in self.store.list_proposals(workspace_id=self.workspace_id, user_id=self.user_id)
                     if p["id"] == proposal_id]
        if not proposals:
            return False
        proposal = proposals[0]
        if not validation or validation.get("success") is not True:
            return False
        changed = self.store.update_proposal(proposal_id, status="active", confidence=confidence, validation=validation)
        if changed:
            self.add_log(proposal["content"], kind=proposal["kind"], status="active", confidence=confidence,
                         metadata={"proposal_id": proposal_id, "validation": validation})
        return changed

    def add_file(self, rel_path: str, content: str) -> str:
        file_id = self.store.upsert_file(
            rel_path, content, user_id=self.user_id, workspace_id=self.file_scope_id,
        )
        text = f"FILE: {rel_path}\n```text\n{content}\n```"
        if self._files_collection is not None:
            try:
                self._files_collection.upsert(
                    ids=[file_id], documents=[text],
                    metadatas=[{"rel_path": rel_path, "content_hash": stable_hash(content),
                                "user_id": self.user_id, "workspace_id": self.file_scope_id}],
                )
            except Exception as exc:
                self._last_error = f"file index write failed: {exc}"
        return file_id

    def remove_stale_files(self, valid_paths: set[str]) -> int:
        removed = self.store.remove_stale_files(
            valid_paths, workspace_id=self.file_scope_id, user_id=self.user_id,
        )
        if self._files_collection is not None:
            try:
                current = self._files_collection.get(include=["metadatas"])
                stale = [doc_id for doc_id, meta in zip(current.get("ids", []), current.get("metadatas", []))
                         if meta.get("user_id") == self.user_id
                         and meta.get("workspace_id") == self.file_scope_id
                         and meta.get("rel_path") not in valid_paths]
                if stale:
                    self._files_collection.delete(ids=stale)
            except Exception as exc:
                self._last_error = f"file index cleanup failed: {exc}"
        return removed

    def _scope_filter(self, row: dict[str, Any]) -> bool:
        return row.get("workspace_id") == self.workspace_id and (
            row.get("session_id") in {None, "", self.session_id}
        )

    def recall(self, query: str, n_results: int = 2, *, include_scoped: bool = False) -> list[str]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(n_results), 100))
        if self._collection is not None:
            try:
                where = {"$and": [
                    {"user_id": self.user_id},
                    {"workspace_id": self.workspace_id},
                    {"status": "active"},
                    {"session_id": self.session_id or ""} if self.session_id is None else {
                        "$or": [{"session_id": ""}, {"session_id": self.session_id}],
                    },
                ]}
                result = self._collection.query(
                    query_texts=[query], n_results=min(limit * 4, max(1, self._collection.count())),
                    where=where,
                )
                docs = result.get("documents", [[]])
                ids = result.get("ids", [[]])
                if docs and docs[0] and ids and ids[0]:
                    rows = self.store.list_memories(status="active", limit=10000,
                                                    workspace_id=self.workspace_id, session_id=self.session_id,
                                                    user_id=self.user_id)
                    by_id = {row["id"]: row for row in rows}
                    scoped_documents = []
                    for memory_id, doc in zip(ids[0], docs[0]):
                        row = by_id.get(memory_id)
                        if row is None or doc.startswith("FILE:"):
                            continue
                        if include_scoped or row.get("metadata", {}).get("visibility", "public") == "public":
                            scoped_documents.append(doc)
                    return scoped_documents[:limit]
            except Exception as exc:
                self._last_error = f"memory index query failed: {exc}"
        rows = self.store.list_memories(status="active", limit=10000, workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        terms = set(re.findall(r"[\w\u3400-\u9fff]+", query.lower()))
        scored = []
        for row in rows:
            if row["kind"] == "source" or row["content"].startswith("FILE:"):
                continue
            if not include_scoped and row.get("metadata", {}).get("visibility", "public") != "public":
                continue
            words = set(re.findall(r"[\w\u3400-\u9fff]+", row["content"].lower()))
            score = len(terms & words)
            if score:
                scored.append((score, row["updated_at"], row["content"]))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:limit]]

    def recall_records(self, query: str, n_results: int = 2, *, profile: str | None = None,
                       task_signature: str | None = None, speaker: str | None = None,
                       audience: str | None = None, channel: str | None = None,
                       authorized_speakers: set[str] | None = None) -> list[dict[str, Any]]:
        """Return recalled data with provenance and trust metadata."""
        # Score a wider candidate window before applying the caller's limit.
        documents = self.recall(query, n_results=max(n_results, 12), include_scoped=True)
        rows = self.store.list_memories(status="active", limit=10000, workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        by_content: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_content.setdefault(row["content"], []).append(row)
        records = [row for doc in documents for row in by_content.get(doc, [])]
        terms = set(re.findall(r"[\w\u3400-\u9fff]+", str(query or "").lower()))
        # The semantic index is derived: it may omit a new product or rank its
        # source observations first. Keep directly matching durable guidance
        # eligible even when it is outside the vector candidate window.
        matching_products = {
            row["id"] for row in rows
            if terms and row.get("metadata", {}).get("learning_feature")
            and row["kind"] != "source" and not row["content"].startswith("FILE:")
            and terms <= set(re.findall(r"[\w\u3400-\u9fff]+", row["content"].lower()))
        }
        records.extend(row for row in rows if row["id"] in matching_products)
        visible = self.filter_records(records, profile=profile, task_signature=task_signature,
                                      speaker=speaker, audience=audience, channel=channel,
                                      authorized_speakers=authorized_speakers)
        visible = list({row["id"]: row for row in visible}.values())
        scoring = self.store.list_events("learning.memory_scored", limit=1,
                                         workspace_id=self.workspace_id, user_id=self.user_id)
        scores = {item["memory_id"]: item["score"] for item in
                  (scoring[0]["payload"].get("scores", []) if scoring else [])}
        if scores:
            visible.sort(key=lambda row: (
                len(terms & set(re.findall(r"[\w\u3400-\u9fff]+", row["content"].lower()))),
                scores.get(row["id"], 0),
            ), reverse=True)
        # Prefer applicable products over the observations they distilled;
        # preserve semantic/importance ordering within each group.
        visible.sort(key=lambda row: row["id"] in matching_products, reverse=True)
        selected = visible[:n_results]
        if selected:
            self.store.append_event("memory.recalled", {
                "query_hash": stable_hash(query), "memory_ids": [row["id"] for row in selected],
                "attributions": [{"memory_id": row["id"],
                                  "speaker": row.get("metadata", {}).get("speaker"),
                                  "claim_type": row.get("metadata", {}).get("claim_type", "general")}
                                 for row in selected],
                "speaker": speaker, "audience": audience, "channel": channel,
            }, user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)
        return selected

    def filter_records(self, records: list[dict[str, Any]], *, profile: str | None = None,
                       task_signature: str | None = None, speaker: str | None = None,
                       audience: str | None = None, channel: str | None = None,
                       authorized_speakers: set[str] | None = None) -> list[dict[str, Any]]:
        context = {"profile": profile, "project": self.workspace_id, "task": task_signature,
                   "speaker": speaker, "audience": audience, "channel": channel}
        authorized_speakers = authorized_speakers or set()
        visible = []
        for row in records:
            scope = row.get("metadata", {}).get("scope")
            if scope and scope.get("type") != "global" and context.get(scope.get("type")) != scope.get("value"):
                continue
            metadata = row.get("metadata", {})
            claim_speaker = metadata.get("speaker")
            if speaker and claim_speaker and claim_speaker != speaker:
                continue
            claim_channel = metadata.get("channel")
            if claim_channel and claim_channel != channel:
                continue
            audiences = set(metadata.get("audiences", []))
            visibility = metadata.get("visibility", "public")
            if visibility == "private" and claim_speaker not in authorized_speakers:
                continue
            if visibility == "group" and audiences and audience not in audiences:
                continue
            visible.append(row)
        return visible

    def get_recent(self, n: int = 10, *, include_scoped: bool = False) -> list[str]:
        rows = self.store.list_memories(status="active", limit=max(1, min(n, 10000)), workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        return [row["content"] for row in rows
                if include_scoped or row.get("metadata", {}).get("visibility", "public") == "public"][:n]

    def count_logs(self) -> int:
        return len(self.store.list_memories(status="active", limit=100000, workspace_id=self.workspace_id,
                                            session_id=self.session_id, user_id=self.user_id))

    def get_all(self, limit: int = 2000) -> tuple[list[str], list[str]]:
        rows = self.store.list_memories(status="active", limit=max(1, min(limit, 10000)), workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        return [row["id"] for row in rows], [row["content"] for row in rows]

    def delete_logs(self, ids: list[str]) -> int:
        """Delete by IDs, accepting exact documents for compatibility with v1 /forget."""
        if not ids:
            return 0
        known_ids, docs = self.get_all(limit=10000)
        resolved = [item if item in known_ids else known_ids[docs.index(item)] for item in ids if item in known_ids or item in docs]
        removed = self.store.delete_memories(resolved, workspace_id=self.workspace_id, user_id=self.user_id)
        if self._collection is not None and resolved:
            try:
                self._collection.delete(ids=resolved)
            except Exception as exc:
                self._last_error = f"memory index delete failed: {exc}"
        return removed

    def _trim_logs(self) -> None:
        if self.MAX_LOGS <= 0:
            return
        rows = self.store.list_memories(status="active", limit=self.MAX_LOGS + 100, workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        if len(rows) <= self.MAX_LOGS:
            return
        self.delete_logs([row["id"] for row in rows[self.MAX_LOGS:]])

    def rebuild_index(self) -> int:
        if self._collection is None:
            return 0
        rows = self.store.list_memories(status="active", limit=100000, workspace_id=self.workspace_id,
                                        session_id=self.session_id, user_id=self.user_id)
        for row in rows:
            if row["kind"] != "source":
                self._index_memory(row["id"], row["content"], kind=row["kind"], status=row["status"])
        return len(rows)

    def claims(self, *, registry=None):
        from .claims import ClaimService
        return ClaimService(self, registry=registry)
