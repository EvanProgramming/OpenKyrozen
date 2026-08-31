"""Local skill registry with manifest and permission validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from event_store import EventStore, stable_hash


ALLOWED_PERMISSIONS = {
    "workspace.read", "workspace.write", "shell", "network", "git.read", "git.write", "browser",
}
LEARNING_PROFILES = {"coder", "researcher"}
LEARNED_STATUSES = {"candidate", "canary", "active", "rolled_back", "rejected"}
MAX_LEARNED_CHARS = 8_000
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[^\s]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


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
        manifest = {
            "name": name, "version": version,
            "description": str(data.get("description", "")).strip()[:500],
            "permissions": sorted(set(str(item) for item in permissions)),
            "entrypoint": str(data.get("entrypoint", "SKILL.md")),
            **data,
        }
        if manifest.get("source") == "learned":
            profiles = manifest.get("profiles", [])
            triggers = manifest.get("triggers", [])
            verification = manifest.get("verification", [])
            artifact_type = manifest.get("artifact_type")
            if artifact_type not in {"policy", "skill"}:
                raise ValueError("learned artifact_type must be policy or skill")
            if not isinstance(profiles, list) or len(profiles) != 1 or profiles[0] not in LEARNING_PROFILES:
                raise ValueError("learned artifacts require exactly one coder or researcher profile")
            if not isinstance(triggers, list) or not 1 <= len(triggers) <= 12:
                raise ValueError("learned artifacts require 1-12 triggers")
            if any(not re.fullmatch(r"[\w.+#-]{2,40}", str(item), re.UNICODE) for item in triggers):
                raise ValueError("invalid learned artifact trigger")
            if not verification or not isinstance(verification, (list, str)):
                raise ValueError("learned artifacts require verification criteria")
            if permissions:
                raise ValueError("learned artifacts cannot grant permissions")
            body = (source / "SKILL.md").read_text(encoding="utf-8", errors="replace")
            if len(body) > MAX_LEARNED_CHARS:
                raise ValueError(f"learned artifact exceeds {MAX_LEARNED_CHARS} characters")
            if _SECRET_RE.search(body + "\n" + json.dumps(manifest, ensure_ascii=False)):
                raise ValueError("learned artifact appears to contain a secret")
            if "DefineTool:" in body:
                raise ValueError("learned artifacts cannot create dynamic tools")
            if any(section not in body for section in ("## Trigger", "## Steps", "## Verify")):
                raise ValueError("learned artifact requires Trigger, Steps, and Verify sections")
        return manifest

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

    def install_learned(self, body: str, manifest: dict[str, Any], *, status: str = "canary") -> dict[str, Any]:
        """Install one immutable, content-addressed learned artifact."""
        if status not in LEARNED_STATUSES:
            raise ValueError("invalid learned artifact status")
        body = str(body).strip()
        data = {**manifest, "source": "learned", "permissions": []}
        data["version"] = f"learned-{stable_hash(body)[:12]}"
        data.setdefault("name", f"learned-{stable_hash(body)[:12]}")
        data.setdefault("description", "Outcome-verified learned guidance")
        staging = Path(tempfile.mkdtemp(prefix=".learned-", dir=self.root))
        try:
            (staging / "SKILL.md").write_text(body + "\n", encoding="utf-8")
            (staging / "skill.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            parsed = self._read_manifest(staging)
            target = self.root / parsed["name"] / parsed["version"]
            existing = next((item for item in self.list()
                             if item["name"] == parsed["name"] and item["version"] == parsed["version"]), None)
            if existing:
                return {"id": existing["id"], "status": existing["status"], "manifest": existing["manifest"],
                        "validation": self.validate(existing["id"]), "existing": True}
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                staging.replace(target)
                staging = None
            skill_id = self.store.upsert_skill(
                name=parsed["name"], version=parsed["version"], path=str(target),
                description=parsed["description"], permissions=[], status=status,
                source="learned", manifest=parsed, workspace_id=self.workspace_id,
            )
            validation = self.validate(skill_id)
            if not validation.get("success"):
                self.store.set_skill_status(skill_id, "rejected", workspace_id=self.workspace_id)
            return {"id": skill_id, "status": status if validation.get("success") else "rejected",
                    "manifest": parsed, "validation": validation}
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

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

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {term.lower() for term in re.findall(r"[\w.+#-]{2,40}", str(text), re.UNICODE)}

    def match(self, profile: str, task: str, *, limit: int = 3, max_chars: int = MAX_LEARNED_CHARS) -> list[dict[str, Any]]:
        """Return active guidance plus at most one matching canary."""
        task_terms = self._terms(task)
        scored: list[tuple[int, dict[str, Any]]] = []
        for skill in self.list():
            manifest = skill.get("manifest", {})
            if skill.get("source") != "learned" or skill.get("status") not in {"active", "canary"}:
                continue
            if manifest.get("profiles") != [profile]:
                continue
            triggers = self._terms(" ".join(str(item) for item in manifest.get("triggers", [])))
            score = len(task_terms & triggers)
            if score:
                scored.append((score, skill))
        canary = [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["name"])) if item["status"] == "canary"][:1]
        canary_names = {item["name"] for item in canary}
        active = [item for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["name"]))
                  if item["status"] == "active" and item["name"] not in canary_names]
        chosen: list[dict[str, Any]] = []
        used = 0
        for skill in (active[:limit] + canary)[:limit]:
            try:
                body = (Path(skill["path"]) / "SKILL.md").read_text(encoding="utf-8")
            except OSError:
                continue
            if used + len(body) > max_chars:
                continue
            chosen.append({**skill, "body": body, "content_hash": stable_hash(body)})
            used += len(body)
        return chosen

    def activate_learned(self, skill_id: str) -> bool:
        skill = next((item for item in self.list() if item["id"] == skill_id), None)
        if not skill or skill.get("source") != "learned" or not self.validate(skill_id).get("success"):
            return False
        for item in self.list("active"):
            if item["id"] != skill_id and item["name"] == skill["name"] and item.get("source") == "learned":
                self.store.set_skill_status(item["id"], "rolled_back", workspace_id=self.workspace_id)
        return self.store.set_skill_status(skill_id, "active", workspace_id=self.workspace_id)

    def set_learned_status(self, skill_id: str, status: str) -> bool:
        if status not in LEARNED_STATUSES:
            return False
        skill = next((item for item in self.list() if item["id"] == skill_id), None)
        return bool(skill and skill.get("source") == "learned" and
                    self.store.set_skill_status(skill_id, status, workspace_id=self.workspace_id))

    def rollback_learned(self, skill_id: str) -> bool:
        skill = next((item for item in self.list() if item["id"] == skill_id), None)
        if not skill or skill.get("source") != "learned":
            return False
        changed = self.store.set_skill_status(skill_id, "rolled_back", workspace_id=self.workspace_id)
        parent = skill.get("manifest", {}).get("parent_version")
        if parent:
            for item in self.list():
                if item["name"] == skill["name"] and item["version"] == parent and item.get("source") == "learned":
                    self.store.set_skill_status(item["id"], "active", workspace_id=self.workspace_id)
        return changed
