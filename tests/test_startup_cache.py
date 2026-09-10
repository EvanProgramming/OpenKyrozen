import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


STARTUP_TIMEOUT_SECONDS = 30
_CREDENTIAL_ENV_VARS = (
    "KYROZEN_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)


class StartupCacheTests(unittest.TestCase):
    def _run_startup(
        self,
        command: list[str],
        *,
        input_text: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> str:
        repository = Path(__file__).parents[1].resolve()
        with tempfile.TemporaryDirectory(
            prefix=".openkyrozen-startup-cache-", dir=repository
        ) as fixture_dir, tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as state_dir:
            # This models a dependency cache below the checkout.  The old
            # startup cleanup deleted it even though OpenKyrozen did not own it.
            sentinel = (
                Path(fixture_dir)
                / "venv"
                / "lib"
                / "python3.12"
                / "site-packages"
                / "dependency"
                / "__pycache__"
                / "dependency.cpython-312.pyc"
            )
            sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b"dependency-bytecode-sentinel")

            env = os.environ.copy()
            env.update(
                {
                    "HOME": home_dir,
                    "KYROZEN_PROVIDER": "ollama",
                    "KYROZEN_DB_PATH": str(Path(state_dir) / "startup.sqlite3"),
                    "KYROZEN_DISABLE_VECTOR_INDEX": "1",
                    "KYROZEN_EXECUTION_SURFACE": "cli",
                    "KYROZEN_TURN_LOG": str(Path(state_dir) / "turns.log"),
                    "PYTHONPATH": os.pathsep.join(
                        part for part in (str(repository), env.get("PYTHONPATH", "")) if part
                    ),
                }
            )
            if extra_env:
                env.update(extra_env)
            for variable in _CREDENTIAL_ENV_VARS:
                env.pop(variable, None)

            started = time.monotonic()
            result = subprocess.run(
                command,
                cwd=repository,
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=STARTUP_TIMEOUT_SECONDS,
            )
            elapsed = time.monotonic() - started

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertLess(
                elapsed,
                STARTUP_TIMEOUT_SECONDS,
                f"startup exceeded {STARTUP_TIMEOUT_SECONDS}s:\n{output}",
            )
            self.assertTrue(sentinel.is_file(), "startup deleted a dependency cache")
            self.assertEqual(sentinel.read_bytes(), b"dependency-bytecode-sentinel")
            return output

    def test_cli_startup_is_bounded_and_preserves_dependency_bytecode(self):
        output = self._run_startup(
            [sys.executable, "main.py"],
            input_text="/quit\n",
            extra_env={
                "KYROZEN_MODEL_SIMPLE": "startup-local-model",
                "KYROZEN_MODEL_COMPLEX": "startup-local-complex-model",
            },
        )
        self.assertIn("Ollama", output)
        self.assertIn("startup-local-model", output)
        self.assertNotIn("DeepSeek V4", output)
        self.assertNotIn("Provider: DeepSeek", output)
        self.assertNotIn("deepseek-chat", output)

    def test_web_startup_is_bounded_and_preserves_dependency_bytecode(self):
        script = (
            "import asyncio; import server; "
            "asyncio.run(server.startup()); "
            "asyncio.run(server.shutdown()); print('web-startup-ready')"
        )
        self._run_startup([sys.executable, "-c", script])

    def test_cli_and_web_can_initialize_one_fresh_database_concurrently(self):
        """Concurrent supported entry points must share a fresh SQLite state safely."""
        repository = Path(__file__).parents[1].resolve()
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as state_dir, \
                tempfile.TemporaryDirectory() as outside_dir:
            env = os.environ.copy()
            env.update({
                "HOME": home_dir,
                "KYROZEN_DB_PATH": str(Path(state_dir) / "concurrent.sqlite3"),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1",
                "KYROZEN_PROVIDER": "ollama",
                "PYTHONPATH": os.pathsep.join(
                    part for part in (str(repository), env.get("PYTHONPATH", "")) if part
                ),
            })
            for variable in _CREDENTIAL_ENV_VARS:
                env.pop(variable, None)
            commands = [
                [sys.executable, str(repository / "main.py"), "--help"],
                [sys.executable, str(repository / "server.py"), "--help"],
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=outside_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for command in commands
            ]
            outputs = []
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=STARTUP_TIMEOUT_SECONDS)
                    output = stdout + stderr
                    outputs.append(output)
                    self.assertEqual(process.returncode, 0, output)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
            combined = "\n".join(outputs)
            self.assertNotIn("database is locked", combined.lower())
            self.assertNotIn("traceback (most recent call last)", combined.lower())
            database = Path(state_dir) / "concurrent.sqlite3"
            self.assertTrue(database.is_file())
            self.assertTrue((database.parent / "concurrent.sqlite3-wal").exists() or database.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
