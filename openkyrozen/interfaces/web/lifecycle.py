from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _load_plugins(self):
    service = self
    """Load the shared Web plugin runtime exactly once."""
    return service._agent._plugin_runtime_for_surface("web").load_once()


def _trigger_hook(self, hook_name: str, **kwargs):
    service = self
    """Compatibility wrapper for startup hooks through the shared runtime."""
    return service._agent._plugin_runtime_for_surface("web").trigger(hook_name, **kwargs)


async def startup(self):
    service = self
    """Initialize the agent on server start."""
    print("[Server] Initializing OpenKyrozen...")
    if service._agent.get_launch_context() is None:
        service._agent.configure_launch_context()
    context = service._agent.get_launch_context()
    if context is not None:
        print(f"[Server] {context.describe()}")
    service._agent._prompt_and_init_deepseek(interactive=False)
    if service._agent.fast_mode.jev_key():
        info = service._agent.fast_mode.jev_model_info(force=True)
        print(f"[Server] System One Jev: {info.get('alias', 'jev-latest')} "
              f"({info.get('release_date') or 'release unknown'}; {info.get('health', 'unknown')})")
    if service._agent.llm_provider is None:
        print("[Server] WARNING: No LLM provider configured. Set API key env vars.")
    else:
        print(f"[Server] Provider: {service._agent._provider_config.provider}")
    service._agent._load_project_files_into_memory()
    service._load_plugins()
    service._trigger_hook(
        "on_startup", agent=service._agent, surface="web", user_id=service._SERVER_ACTOR_ID,
        workspace_id=service._agent.memory_bank.workspace_id,
    )
    recovered = service._recover_task_scopes()
    if recovered:
        service._audit("TASK_RECOVERY", f"recovered={len(recovered)} pending_or_resumable tasks")
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in service._scheduler.list_jobs()):
        service._scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    if (service._agent.learning_runtime()["status"] == "ready"
            and not any(job.get("payload", {}).get("type") == "learning_cycle" for job in service._scheduler.list_jobs())):
        service._scheduler.schedule_every("learning-cycle", 30.0, payload={"type": "learning_cycle"},
                                  job_id="job_learning_cycle", delay_seconds=0)
    service._scheduler.start()
    service._audit("STARTUP", "server started")
    print("[Server] Ready — Uvicorn will report the effective bind address (set KYROZEN_SERVER_TOKEN for remote access)")


async def shutdown(self):
    service = self
    service._scheduler.stop()
    if service._owns_application and service._application is not None:
        service._application.close()
        service._application = None


def _server_parser(self):
    service = self
    import argparse

    parser = argparse.ArgumentParser(description="OpenKyrozen Web Server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--project", metavar="PATH", help="operate directly on this project directory")
    mode.add_argument("--global", dest="global_mode", action="store_true",
                      help="use the persistent global workspace (the default)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    return parser


def _parse_server_args(self, argv: list[str] | None = None):
    service = self
    args = service._server_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        context = service._agent.configure_launch_context(
            project_path=args.project,
            global_mode=args.global_mode,
        )
    except ValueError as exc:
        service._server_parser().error(str(exc))
    return args, context


def main_entry(self):
    service = self
    """Entry point for pyproject.toml console_scripts."""
    args, context = service._parse_server_args()
    if args.reload:
        # Uvicorn imports the app in a child process when reloading. Carry the
        # already-resolved mode across that import without using process cwd.
        os.environ["KYROZEN_WORKSPACE_ROOT"] = str(context.active_root)
        os.environ["KYROZEN_LAUNCH_MODE"] = context.mode
        uvicorn.run(
            "openkyrozen.interfaces.web.app:app", host=args.host, port=args.port, reload=True,
            app_dir=str(Path(__file__).resolve().parents[3]),
        )
        return
    uvicorn.run(service.app, host=args.host, port=args.port, reload=False)
