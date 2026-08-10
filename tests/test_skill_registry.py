import json
import tempfile
import unittest
from pathlib import Path

from event_store import EventStore
from skill_registry import SkillRegistry


class SkillRegistryTests(unittest.TestCase):
    def test_skill_install_validates_permissions_and_requires_explicit_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
            (source / "skill.json").write_text(json.dumps({
                "name": "test-skill", "version": "1.0.0", "description": "test",
                "permissions": ["workspace.read"],
            }), encoding="utf-8")
            registry = SkillRegistry(EventStore(root / "state.sqlite3"), root=root / "installed")
            installed = registry.install(source)
            self.assertEqual(installed["status"], "candidate")
            self.assertTrue(registry.activate(installed["id"]))
            self.assertEqual(registry.list("active")[0]["name"], "test-skill")
            self.assertTrue(registry.rollback(installed["id"]))

    def test_skill_rejects_undeclared_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("# Test\n", encoding="utf-8")
            (source / "skill.json").write_text(json.dumps({"name": "bad-skill", "permissions": ["secrets.read"]}), encoding="utf-8")
            registry = SkillRegistry(EventStore(root / "state.sqlite3"), root=root / "installed")
            with self.assertRaises(ValueError):
                registry.install(source)


if __name__ == "__main__":
    unittest.main()
