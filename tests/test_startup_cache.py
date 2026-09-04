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
    def _run_startup(self, command: list[str], *, input_text: str = "") -> None:
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

    def test_cli_startup_is_bounded_and_preserves_dependency_bytecode(self):
        self._run_startup([sys.executable, "main.py"], input_text="/quit\n")

    def test_web_startup_is_bounded_and_preserves_dependency_bytecode(self):
        script = (
            "import asyncio; import server; "
            "asyncio.run(server.startup()); "
            "asyncio.run(server.shutdown()); print('web-startup-ready')"
        )
        self._run_startup([sys.executable, "-c", script])


if __name__ == "__main__":
    unittest.main()
