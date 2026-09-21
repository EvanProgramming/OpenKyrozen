import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from project_graph import ProjectGraph


class FakeGraphify:
    def __init__(self):
        self.commands = []
        self.fail = False

    def __call__(self, command, **kwargs):
        if command[0] == "git":
            return subprocess.run(command, **kwargs)
        self.commands.append(command[3:])
        cwd = Path(kwargs["cwd"])
        output = cwd / "graphify-out" / "graph.json"
        if command[3] in {"extract", "update"}:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({
                "nodes": [
                    {"id": "app", "label": "app", "community": 1,
                     "source_file": str(cwd / "app.py"), "source_location": "1:1"},
                    {"id": "helper", "label": "helper", "community": 2,
                     "source_file": str(cwd / "helper.py")},
                ],
                "links": [{"source": "app", "target": "helper", "relation": "calls"}],
            }), encoding="utf-8")
            if self.fail:
                output.write_text("corrupt", encoding="utf-8")
                return subprocess.CompletedProcess(command, 1, "", "failed update")
            return subprocess.CompletedProcess(command, 0, "indexed", "")
        return subprocess.CompletedProcess(command, 0, f"result at {cwd / 'app.py'}", "")


class ProjectGraphTests(unittest.TestCase):
    def test_git_incremental_sync_ignores_files_and_removes_vanished_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            state = Path(directory) / "state"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (root / "app.py").write_text("print('one')\n", encoding="utf-8")
            (root / "helper.py").write_text("def helper(): pass\n", encoding="utf-8")
            (root / "ignored.py").write_text("secret = True\n", encoding="utf-8")
            outside = Path(directory) / "outside.py"
            outside.write_text("outside = True\n", encoding="utf-8")
            (root / "escape.py").symlink_to(outside)
            fake = FakeGraphify()
            graph = ProjectGraph(root, state, "scope", runner=fake)

            first = graph.refresh()
            self.assertEqual(first["status"], "ready")
            self.assertTrue((graph.mirror_root / "app.py").is_file())
            self.assertFalse((graph.mirror_root / "ignored.py").exists())
            self.assertFalse((graph.mirror_root / "escape.py").exists())
            self.assertEqual(graph.sync()["copied"], 0)

            before = (root / "app.py").stat().st_mtime_ns
            (root / "app.py").write_text("print('two')\n", encoding="utf-8")
            os.utime(root / "app.py", ns=(before + 1_000_000, before + 1_000_000))
            self.assertEqual(graph.sync()["copied"], 1)
            (root / "helper.py").unlink()
            self.assertEqual(graph.sync()["deleted"], 1)
            self.assertFalse((graph.mirror_root / "helper.py").exists())

    def test_failed_update_restores_graph_and_remaps_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "app.py").write_text("one = 1\n", encoding="utf-8")
            fake = FakeGraphify()
            graph = ProjectGraph(root, Path(directory) / "state", "scope", runner=fake)
            self.assertEqual(graph.refresh()["status"], "ready")
            valid = graph.graph_path.read_text(encoding="utf-8")
            result = graph.query("where is app")
            self.assertIn(str(root), result)
            self.assertNotIn(str(graph.mirror_root), result)

            (root / "app.py").write_text("two = 2\n", encoding="utf-8")
            fake.fail = True
            failed = graph.refresh()
            self.assertEqual(failed["status"], "stale")
            self.assertEqual(graph.graph_path.read_text(encoding="utf-8"), valid)

    def test_full_rebuild_non_git_walk_and_concurrent_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plain"
            root.mkdir()
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "skip.js").write_text("skip", encoding="utf-8")
            fake = FakeGraphify()
            graph = ProjectGraph(root, Path(directory) / "state", "scope", runner=fake)
            graph.refresh(full=True)
            self.assertEqual(fake.commands[0][0], "extract")
            self.assertFalse((graph.mirror_root / "node_modules").exists())

            results = []
            threads = [threading.Thread(target=lambda: results.append(graph.query("app"))) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), 4)
            self.assertTrue(all(str(root) in result for result in results))


if __name__ == "__main__":
    unittest.main()
