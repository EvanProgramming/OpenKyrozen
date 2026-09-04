import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workspace_context import resolve_launch_context, source_scope_id


class WorkspaceContextTests(unittest.TestCase):
    def test_bare_launch_uses_private_global_workspace_independent_of_caller(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as caller:
            context = resolve_launch_context(home=home, cwd=caller)
            expected = Path(home) / ".kyrozen" / "workspace"
            self.assertEqual(context.mode, "global")
            self.assertEqual(context.active_root, expected.resolve())
            self.assertTrue(expected.is_dir())
            self.assertNotEqual(context.active_root, Path(caller).resolve())

    def test_project_launch_uses_the_origin_directory_and_stable_file_scope(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as project:
            project_path = Path(project).resolve()
            context = resolve_launch_context(home=home, cwd=home, project_path=project_path)
            self.assertEqual(context.mode, "project")
            self.assertEqual(context.active_root, project_path)
            self.assertEqual(context.source_scope_id, source_scope_id(project_path))
            self.assertEqual(context.requested_project, project_path)

    def test_conflicting_modes_and_missing_projects_are_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(ValueError):
                resolve_launch_context(home=home, project_path=home, global_mode=True)
            with self.assertRaises(ValueError):
                resolve_launch_context(home=home, project_path=Path(home) / "missing")

    def test_explicit_cli_modes_take_precedence_over_development_override(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as override:
            environ = {"KYROZEN_WORKSPACE_ROOT": override}
            project_context = resolve_launch_context(
                home=home, cwd=home, project_path=project, environ=environ,
            )
            global_context = resolve_launch_context(
                home=home, cwd=project, global_mode=True, environ=environ,
            )
            self.assertEqual(project_context.mode, "project")
            self.assertEqual(project_context.active_root, Path(project).resolve())
            self.assertEqual(global_context.mode, "global")
            self.assertEqual(global_context.active_root, (Path(home) / ".kyrozen" / "workspace").resolve())

    def test_global_cli_starts_from_an_unrelated_directory(self):
        repository = Path(__file__).parents[1].resolve()
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as caller:
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "KYROZEN_PROVIDER": "ollama",
                "KYROZEN_DB_PATH": str(Path(state) / "state.sqlite3"),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            })
            result = subprocess.run(
                [sys.executable, str(repository / "main.py")],
                cwd=caller,
                env=env,
                input="/quit\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("Global mode:", output)
            self.assertIn(str((Path(home) / ".kyrozen" / "workspace").resolve()), output.replace("\n", ""))
            self.assertNotIn(f"Global mode: tools operate in `{caller}`", output)

    def test_project_init_binds_the_origin_before_provider_setup(self):
        repository = Path(__file__).parents[1].resolve()
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as project:
            project_path = Path(project).resolve()
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "KYROZEN_PROVIDER": "ollama",
                "KYROZEN_DB_PATH": str(Path(state) / "state.sqlite3"),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            })
            result = subprocess.run(
                [sys.executable, str(repository / "main.py"), "--project", str(project_path), "--init"],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("Project mode:", output)
            self.assertIn(str(project_path), output)


if __name__ == "__main__":
    unittest.main()
