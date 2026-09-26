"""Per-session conversation history trees and workspace snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from event_store import EventStore, utc_now


class HistoryError(RuntimeError):
    """Raised when a history operation cannot be completed safely."""


@dataclass(frozen=True)
class TurnToken:
    parent_id: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HistoryManager:
    """Own immutable history nodes while leaving durable memory untouched."""

    def __init__(self, store: EventStore, root: str | os.PathLike[str], state_root: str | os.PathLike[str],
                 *, source_scope_id: str, user_id: str = "local", workspace_id: str = "default",
                 session_id: str):
        self.store = store
        self.root = Path(root).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.config_path = self.state_root.parent.parent / ".kyrozen_config.json"
        if not re.fullmatch(r"source-[a-f0-9]{32}", str(source_scope_id)):
            raise HistoryError("invalid source scope")
        self.source_scope_id = str(source_scope_id)
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.history_root = self.state_root / "history" / source_scope_id / self._safe_component(session_id)
        self._lock = threading.RLock()

    @staticmethod
    def _safe_component(value: str) -> str:
        value = str(value)
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
            return value
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def _node_path(self, node_id: str) -> Path:
        if not re.fullmatch(r"hist_[a-f0-9]{32}", node_id):
            raise HistoryError("invalid history node")
        return self.history_root / node_id

    def _excluded(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        if ".git" in relative.parts:
            return True
        try:
            if path == self.state_root or path.is_relative_to(self.state_root):
                return True
        except AttributeError:  # pragma: no cover - Python 3.12+ has is_relative_to
            try:
                path.relative_to(self.state_root)
                return True
            except ValueError:
                pass
        if path == self.config_path:
            return True
        return False

    def _snapshot(self, node_id: str) -> tuple[str, dict[str, Any], dict[str, int]]:
        # ponytail: full tree copy per node; add content-addressed dedupe only if measured size requires it.
        final = self._node_path(node_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.parent / f".{node_id}.tmp-{uuid.uuid4().hex}"
        workspace = temporary / "workspace"
        workspace.mkdir(parents=True)
        manifest: dict[str, Any] = {}
        try:
            for base, directories, files in os.walk(self.root, topdown=True, followlinks=False):
                base_path = Path(base)
                kept_directories: list[str] = []
                for name in sorted(directories):
                    source = base_path / name
                    if self._excluded(source):
                        continue
                    relative = source.relative_to(self.root).as_posix()
                    if source.is_symlink():
                        target = os.readlink(source)
                        manifest[relative] = {"kind": "symlink", "target": target}
                        destination = workspace / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(target, destination)
                    else:
                        kept_directories.append(name)
                        destination = workspace / relative
                        destination.mkdir(parents=True, exist_ok=True)
                        manifest[relative] = {
                            "kind": "dir", "mode": stat.S_IMODE(source.stat().st_mode),
                        }
                directories[:] = kept_directories
                for name in sorted(files):
                    source = base_path / name
                    if self._excluded(source):
                        continue
                    relative = source.relative_to(self.root).as_posix()
                    destination = workspace / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_symlink():
                        manifest[relative] = {"kind": "symlink", "target": os.readlink(source)}
                        os.symlink(os.readlink(source), destination)
                    elif source.is_file():
                        shutil.copy2(source, destination)
                        manifest[relative] = {
                            "kind": "file", "size": destination.stat().st_size,
                            "mode": stat.S_IMODE(destination.stat().st_mode),
                            "digest": _digest(destination),
                        }
                    else:
                        raise HistoryError(f"unsupported workspace entry: {relative}")
            manifest_path = temporary / "manifest.json"
            manifest_path.write_text(_json(manifest), encoding="utf-8")
            os.replace(temporary, final)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        files_count = sum(item.get("kind") in {"file", "symlink"} for item in manifest.values())
        summary = {"files": files_count, "directories": sum(item.get("kind") == "dir" for item in manifest.values())}
        return self._relative_snapshot(final), manifest, summary

    def _relative_snapshot(self, path: Path) -> str:
        return path.relative_to(self.state_root).as_posix()

    def _snapshot_path(self, node: dict[str, Any]) -> Path:
        path = self.state_root / str(node["snapshot_relpath"])
        try:
            path.relative_to(self.state_root)
        except ValueError as exc:
            raise HistoryError("history snapshot escaped the state root") from exc
        return path

    def _manifest(self, node: dict[str, Any]) -> dict[str, Any]:
        path = self._snapshot_path(node) / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HistoryError("history snapshot is unavailable or corrupt") from exc
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
        before_files = {key: value for key, value in before.items() if value.get("kind") != "dir"}
        after_files = {key: value for key, value in after.items() if value.get("kind") != "dir"}
        added = set(after_files) - set(before_files)
        deleted = set(before_files) - set(after_files)
        changed = {
            key for key in set(before_files) & set(after_files)
            if before_files[key] != after_files[key]
        }
        return {"added": len(added), "changed": len(changed), "deleted": len(deleted),
                "total": len(added) + len(changed) + len(deleted)}

    def _node(self, *, node_id: str, parent_id: str | None, kind: str, summary: str,
              user_message: str, assistant_message: str, conversation: list[dict[str, Any]],
              interaction: dict[str, Any], tasks: list[dict[str, Any]], snapshot_relpath: str,
              file_summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": node_id, "parent_id": parent_id, "kind": kind, "summary": summary[:240],
            "user_message": user_message[:12000], "assistant_message": assistant_message[:12000],
            "conversation": conversation[-32:], "interaction": interaction, "tasks": tasks,
            "snapshot_relpath": snapshot_relpath, "file_summary": file_summary,
            "user_id": self.user_id, "workspace_id": self.workspace_id, "session_id": self.session_id,
            "created_at": utc_now(),
        }

    def ensure_root(self, *, conversation: list[dict[str, Any]] | None = None,
                    interaction: dict[str, Any] | None = None, tasks: list[dict[str, Any]] | None = None,
                    legacy: bool = False) -> dict[str, Any]:
        with self._lock:
            head_id = self.store.history_head(user_id=self.user_id, workspace_id=self.workspace_id,
                                              session_id=self.session_id)
            if head_id:
                node = self.store.history_node(head_id, user_id=self.user_id, workspace_id=self.workspace_id,
                                               session_id=self.session_id)
                if node:
                    return node
            node_id = f"hist_{uuid.uuid4().hex}"
            snapshot, manifest, summary = self._snapshot(node_id)
            node = self._node(
                node_id=node_id, parent_id=None, kind="baseline",
                summary="History tracking started" if legacy else "Workspace baseline",
                user_message="", assistant_message="", conversation=conversation or [],
                interaction=interaction or {}, tasks=tasks or [], snapshot_relpath=snapshot,
                file_summary={**summary, "changes": {"added": 0, "changed": 0, "deleted": 0, "total": 0},
                              "manifest_digest": hashlib.sha256(_json(manifest).encode()).hexdigest()},
            )
            self.store.insert_history_node(node, set_head=True)
            return node

    def begin_turn(self, *, conversation: list[dict[str, Any]], interaction: dict[str, Any],
                   tasks: list[dict[str, Any]]) -> TurnToken:
        root = self.ensure_root(conversation=conversation, interaction=interaction, tasks=tasks)
        return TurnToken(parent_id=root["id"])

    def commit_turn(self, token: TurnToken, *, user_message: str, assistant_message: str,
                    conversation: list[dict[str, Any]], interaction: dict[str, Any],
                    tasks: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            parent = self.store.history_node(token.parent_id, user_id=self.user_id,
                                             workspace_id=self.workspace_id, session_id=self.session_id)
            if parent is None:
                raise HistoryError("history parent no longer exists")
            node_id = f"hist_{uuid.uuid4().hex}"
            snapshot, manifest, summary = self._snapshot(node_id)
            parent_manifest = self._manifest(parent)
            node = self._node(
                node_id=node_id, parent_id=parent["id"], kind="turn",
                summary=(user_message.strip().splitlines() or ["Completed turn"])[0],
                user_message=user_message, assistant_message=assistant_message,
                conversation=conversation, interaction=interaction, tasks=tasks,
                snapshot_relpath=snapshot,
                file_summary={**summary, "changes": self._changes(parent_manifest, manifest),
                              "manifest_digest": hashlib.sha256(_json(manifest).encode()).hexdigest()},
            )
            if not self.store.insert_history_node(node, set_head=True, expected_head=token.parent_id):
                shutil.rmtree(self._node_path(node_id), ignore_errors=True)
                raise HistoryError("history head changed while recording the turn")
            return node

    def list(self, *, include_recovery: bool = False) -> list[dict[str, Any]]:
        return self.store.list_history_nodes(user_id=self.user_id, workspace_id=self.workspace_id,
                                             session_id=self.session_id, include_recovery=include_recovery)

    def current(self) -> dict[str, Any] | None:
        head = self.store.history_head(user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)
        return self.store.history_node(head, user_id=self.user_id, workspace_id=self.workspace_id,
                                       session_id=self.session_id) if head else None

    def _remove_path(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    def _restore(self, node: dict[str, Any]) -> None:
        snapshot_workspace = self._snapshot_path(node) / "workspace"
        manifest = self._manifest(node)
        if not snapshot_workspace.is_dir():
            raise HistoryError("history workspace snapshot is missing")
        for base, directories, files in os.walk(self.root, topdown=False, followlinks=False):
            base_path = Path(base)
            for name in files + directories:
                path = base_path / name
                if self._excluded(path):
                    continue
                relative = path.relative_to(self.root).as_posix()
                if relative not in manifest:
                    self._remove_path(path)
        for relative, item in sorted(manifest.items(), key=lambda pair: (pair[1].get("kind") != "dir", pair[0])):
            destination = self.root / relative
            kind = item.get("kind")
            if kind == "dir":
                if destination.exists() and not destination.is_dir():
                    self._remove_path(destination)
                destination.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(destination, int(item.get("mode", 0o755)), follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
            elif kind == "symlink":
                if destination.exists() or destination.is_symlink():
                    self._remove_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(item.get("target", ""), destination)
            elif kind == "file":
                if destination.exists() or destination.is_symlink():
                    self._remove_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot_workspace / relative, destination)

    def rollback(self, node_id: str, *, confirm: str, expected_head: str | None = None,
                 current_conversation: list[dict[str, Any]] | None = None,
                 current_interaction: dict[str, Any] | None = None,
                 current_tasks: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if confirm != "rollback":
            raise HistoryError('rollback requires confirm="rollback"')
        with self._lock:
            target = self.store.history_node(node_id, user_id=self.user_id, workspace_id=self.workspace_id,
                                             session_id=self.session_id)
            current = self.current()
            if target is None or current is None:
                raise HistoryError("history node not found")
            if expected_head is not None and current["id"] != expected_head:
                raise HistoryError("history head changed; reload history and try again")
            recovery_id = f"hist_{uuid.uuid4().hex}"
            recovery_snapshot, recovery_manifest, recovery_summary = self._snapshot(recovery_id)
            recovery = self._node(
                node_id=recovery_id, parent_id=current["id"], kind="recovery", summary="Before rollback",
                user_message="", assistant_message="",
                conversation=current_conversation if current_conversation is not None else current["conversation"],
                interaction=current_interaction if current_interaction is not None else current["interaction"],
                tasks=current_tasks if current_tasks is not None else current["tasks"],
                snapshot_relpath=recovery_snapshot, file_summary={**recovery_summary,
                    "changes": self._changes(self._manifest(current), recovery_manifest)},
            )
            self.store.insert_history_node(recovery)
            head_switched = False
            try:
                self._restore(target)
                if not self.store.set_history_head(target["id"], user_id=self.user_id, workspace_id=self.workspace_id,
                                                   session_id=self.session_id, expected_head=current["id"]):
                    raise HistoryError("history head changed; reload history and try again")
                head_switched = True
                self.store.replace_tasks(target["tasks"], user_id=self.user_id, workspace_id=self.workspace_id,
                                         session_id=self.session_id)
                self.store.append_event(
                    "history.rollback", {"target_id": target["id"], "recovery_id": recovery_id},
                    user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id,
                )
                self.store.append_event(
                    "history.restored", {"node_id": target["id"], "interaction": target["interaction"]},
                    user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id,
                )
            except Exception:
                try:
                    self._restore(recovery)
                    if head_switched:
                        self.store.set_history_head(
                            current["id"], user_id=self.user_id, workspace_id=self.workspace_id,
                            session_id=self.session_id, expected_head=target["id"],
                        )
                        self.store.replace_tasks(
                            recovery["tasks"], user_id=self.user_id, workspace_id=self.workspace_id,
                            session_id=self.session_id,
                        )
                except Exception as recovery_error:
                    raise HistoryError(f"rollback failed and recovery failed: {recovery_error}") from recovery_error
                raise
            return target, recovery

    def tree_text(self) -> str:
        nodes = self.list()
        current_id = self.current()["id"] if self.current() else None
        children: dict[str | None, list[dict[str, Any]]] = {}
        for node in nodes:
            children.setdefault(node.get("parent_id"), []).append(node)
        lines: list[str] = []
        def walk(parent: str | None, prefix: str = "") -> None:
            for node in children.get(parent, []):
                marker = "*" if node["id"] == current_id else " "
                changes = node.get("file_summary", {}).get("changes", {})
                delta = f" +{changes.get('added', 0)} ~{changes.get('changed', 0)} -{changes.get('deleted', 0)}"
                stamp = node.get("created_at", "").replace("T", " ")[:19]
                lines.append(f"{prefix}{marker} {node['id']} {stamp} {node.get('summary') or 'baseline'}{delta}")
                walk(node["id"], prefix + "  ")
        walk(None)
        return "\n".join(lines) if lines else "No history nodes recorded yet."
