"""Maintained debug entry point for OpenKyrozen.

The default command is a deterministic, non-interactive smoke test.  It uses
an isolated temporary database and workspace, exercises provider startup,
configuration loading, the real file tools, and the active Python interpreter,
then exits with a useful status code.  It never asks for an API key.

Use ``python main_debug.py --interactive`` when an interactive CLI session is
wanted; that mode intentionally follows the normal credential prompt flow.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import tempfile
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an isolated OpenKyrozen diagnostic smoke test."
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="start the normal interactive CLI instead of the smoke test",
    )
    return parser


def _path_from_tool_output(output: str) -> Path | None:
    candidate = output.strip().splitlines()[-1] if output.strip() else ""
    if not candidate or candidate.startswith(("Error", "Exit code")):
        return None
    try:
        return Path(candidate).expanduser().resolve()
    except (OSError, ValueError):
        return None


def run_smoke() -> int:
    """Run the real local startup and tool path without external services."""
    # Keep this diagnostic process independent of personal credentials and
    # runtime state.  The environment changes affect only this child process.
    os.environ["KYROZEN_PROVIDER"] = "ollama"
    os.environ.pop("KYROZEN_API_KEY", None)
    for variable in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(variable, None)
    os.environ["KYROZEN_EXECUTION_SURFACE"] = "cli"
    os.environ["KYROZEN_DISABLE_VECTOR_INDEX"] = "1"

    repository_root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="openkyrozen-debug-state-") as state_dir, \
            tempfile.TemporaryDirectory(prefix="openkyrozen-debug-workspace-") as workspace_dir:
        os.environ["KYROZEN_DB_PATH"] = str(Path(state_dir) / "debug.sqlite3")

        import main as agent
        from agent_config import load_agent_config

        workspace = Path(workspace_dir)
        agent._set_workspace_root(str(workspace))
        failures: list[str] = []

        config = load_agent_config(repository_root)
        if config["role"]["name"] != "assistant":
            failures.append("packaged agent role did not load")
        if not config["examples"]:
            failures.append("packaged prompt examples did not load")

        provider_initialized = agent._prompt_and_init_deepseek(interactive=False)
        if not provider_initialized or agent.llm_provider is None:
            failures.append("keyless Ollama startup did not initialise")

        write_result = agent._run_tool("write_file", "debug-smoke.txt|debug-ok")
        smoke_file = workspace / "debug-smoke.txt"
        if not smoke_file.is_file() or smoke_file.read_text(encoding="utf-8") != "debug-ok":
            failures.append(f"write_file had no observable effect: {write_result}")

        read_result = agent._run_tool("read_file", "debug-smoke.txt")
        if read_result != "debug-ok":
            failures.append(f"read_file returned {read_result!r}")

        interpreter_result = agent._run_tool(
            "run_cmd", "python -c 'import sys; print(sys.executable)'"
        )
        actual_interpreter = _path_from_tool_output(interpreter_result)
        expected_interpreter = Path(sys.executable).resolve()
        if actual_interpreter != expected_interpreter:
            failures.append(
                f"run_cmd used {actual_interpreter}, expected {expected_interpreter}"
            )

        messages = agent._build_messages("debug smoke")
        if not messages or "## Available Tools" not in messages[0]["content"]:
            failures.append("the normal prompt/tool inventory did not build")

        report = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "interpreter": str(expected_interpreter),
            "provider": agent._provider_config.provider if agent._provider_config else None,
            "provider_initialized": bool(provider_initialized),
            "workspace_tool_effect": smoke_file.is_file() and read_result == "debug-ok",
            "memory_path_isolated": str(agent.memory_bank.db_path).startswith(state_dir),
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if failures:
            print("OpenKyrozen debug smoke failed.", file=sys.stderr)
            return 1
        print("OpenKyrozen debug smoke passed.")
        return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch either the safe smoke test or the explicitly interactive CLI."""
    faulthandler.enable()
    args = _parser().parse_args(argv)
    if args.interactive:
        import main as agent
        agent.main()
        return 0
    return run_smoke()


if __name__ == "__main__":
    raise SystemExit(main())
