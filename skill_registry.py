"""Local skill registry with manifest and permission validation."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from event_store import EventStore


ALLOWED_PERMISSIONS = {
    "workspace.read", "workspace.write", "shell", "network", "git.read", "git.write", "browser",
}


class SkillRegistry:
    def __init__(self, store: EventStore, *, workspace_id: str = "default", root: str | os.PathLike[str] | None = None):
        self.store = store
        self.workspace_id = workspace_id
        self.root = Path(root or os.environ.get("KYROZEN_SKILLS_DIR", Path.home() / ".kyrozen" / "v2" / "skills")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_manifest(source: Path) -> dict[str, Any]:
        manifest_path = source / "skill.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("skill.json must contain an object")
        else:
            data = {}
        if not (source / "SKILL.md").is_file():
            raise ValueError("Skill must contain SKILL.md")
        name = str(data.get("name", source.name)).strip()
        version = str(data.get("version", "0.1.0")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,63}", name):
            raise ValueError("Invalid skill name")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z+_.-]{0,31}", version):
            raise ValueError("Invalid skill version")
        permissions = data.get("permissions", [])
        if not isinstance(permissions, list) or any(str(item) not in ALLOWED_PERMISSIONS for item in permissions):
            raise ValueError(f"permissions must be a list from {sorted(ALLOWED_PERMISSIONS)}")
        return {
            "name": name, "version": version,
            "description": str(data.get("description", "")).strip()[:500],
            "permissions": sorted(set(str(item) for item in permissions)),
            "entrypoint": str(data.get("entrypoint", "SKILL.md")),
            **data,
        }

    def install(self, source: str | os.PathLike[str], *, activate: bool = False, source_name: str = "local") -> dict[str, Any]:
        source_path = Path(source).expanduser().resolve(strict=True)
        if not source_path.is_dir():
            raise ValueError("Skill source must be a directory")
        for child in source_path.rglob("*"):
            if child.is_symlink():
                raise ValueError("Symlinked skill files are not allowed")
        manifest = self._read_manifest(source_path)
        target = self.root / manifest["name"] / manifest["version"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)
        status = "candidate"
        skill_id = self.store.upsert_skill(
            name=manifest["name"], version=manifest["version"], path=str(target),
            description=manifest["description"], permissions=manifest["permissions"],
            status=status, source=source_name, manifest=manifest, workspace_id=self.workspace_id,
        )
        validation = self.validate(skill_id)
        if activate and validation["success"]:
            self.store.set_skill_status(skill_id, "active", workspace_id=self.workspace_id)
            status = "active"
        return {"id": skill_id, "status": status, "manifest": manifest, "validation": validation}

    def validate(self, skill_id: str) -> dict[str, Any]:
        skills = [skill for skill in self.store.list_skills(workspace_id=self.workspace_id, limit=5000) if skill["id"] == skill_id]
        if not skills:
            return {"success": False, "error": "skill not found"}
        skill = skills[0]
        path = Path(skill["path"]).resolve()
        try:
            path.relative_to(self.root)
            manifest = self._read_manifest(path)
            return {"success": True, "checks": ["contained path", "SKILL.md", "manifest", "declared permissions"],
                    "name": manifest["name"], "version": manifest["version"]}
        except Exception as exc:
            return {"success": False, "error": str(exc)[:500]}

    def activate(self, skill_id: str) -> bool:
        validation = self.validate(skill_id)
        if not validation.get("success"):
            return False
        return self.store.set_skill_status(skill_id, "active", workspace_id=self.workspace_id)

    def rollback(self, skill_id: str) -> bool:
        return self.store.set_skill_status(skill_id, "rolled_back", workspace_id=self.workspace_id)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_skills(workspace_id=self.workspace_id, status=status)
