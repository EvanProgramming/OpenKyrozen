import json
import tempfile
import unittest
from pathlib import Path

from event_store import EventStore
from learning_engine import LearningEngine
from memory import MemoryBank
import main
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
                "permissions": ["workspace.read"], "profiles": ["coder"], "triggers": ["pytest"],
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

    def test_runtime_local_skill_requires_profiles_and_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("# Local skill\n", encoding="utf-8")
            (source / "skill.json").write_text(json.dumps({
                "name": "missing-runtime-fields", "version": "1.0.0", "permissions": [],
            }), encoding="utf-8")
            registry = SkillRegistry(EventStore(root / "state.sqlite3"), root=root / "installed")
            with self.assertRaisesRegex(ValueError, "profiles"):
                registry.install(source)

    def test_active_local_skill_reaches_runtime_prompt_with_auditable_receipt(self):
        original_memory = main.memory_bank
        original_registry = main.skill_registry
        original_engine = main.learning_engine
        original_root = main._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            body = """# Local pytest guidance

## Trigger
Use this for pytest tasks.

## Steps
1. Run the focused test first.

## Verify
Confirm the test result before reporting success.
"""
            (source / "SKILL.md").write_text(body, encoding="utf-8")
            (source / "skill.json").write_text(json.dumps({
                "name": "local-pytest-guide", "version": "1.0.0",
                "description": "A local runtime guide", "permissions": [],
                "profiles": ["coder"], "triggers": ["pytest"],
            }), encoding="utf-8")
            memory = MemoryBank(root / "state.sqlite3", user_id="skill-user",
                                workspace_id="skill-workspace", session_id="skill-session")
            registry = SkillRegistry(memory.store, workspace_id="skill-workspace", root=root / "installed")
            engine = LearningEngine(memory, registry=registry)
            installed = registry.install(source, activate=True)
            self.assertEqual(installed["status"], "active")
            run = engine.begin_run("coder", "run pytest for this change")
            context, receipts = engine.artifact_context(run)
            self.assertIn("Local pytest guidance", context)
            self.assertEqual(receipts[0]["source"], "local")
            self.assertEqual(registry.match("researcher", "run pytest"), [])

            main.memory_bank = memory
            main.skill_registry = registry
            main.learning_engine = engine
            main._set_workspace_root(root)
            try:
                messages = main._build_messages("run pytest", context)
            finally:
                main.memory_bank = original_memory
                main.skill_registry = original_registry
                main.learning_engine = original_engine
                main._set_workspace_root(original_root)
            prompt = "\n".join(item["content"] for item in messages)
            self.assertIn(body, prompt)
            self.assertIn("cannot grant permissions", prompt)
            events = memory.store.list_events(
                "learning.artifact_used", workspace_id="skill-workspace", session_id="skill-session"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["source"], "local")


if __name__ == "__main__":
    unittest.main()
