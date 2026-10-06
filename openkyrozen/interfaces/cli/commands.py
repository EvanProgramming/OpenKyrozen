from __future__ import annotations

import json
import os
import sys
import threading
from openkyrozen.memory.service import MemoryBank
from openkyrozen.agent.modes import split_inline_command
import openkyrozen.routing.system_one as fast_mode
from openkyrozen.providers import get_cost_summary

def main(self) -> None:

    argv = sys.argv[1:]

    if len(argv) >= 2 and argv[0].lower() == "migrate" and argv[1].lower() == "v1":
        from openkyrozen.persistence.migration import migrate_v1_chroma
        source = argv[2] if len(argv) > 2 else "./chroma_memory"
        target = os.environ.get("KYROZEN_DB_PATH", MemoryBank.DEFAULT_PATH)
        report = migrate_v1_chroma(source, target, workspace_id=os.environ.get("KYROZEN_WORKSPACE_ID", "default"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if len(argv) >= 2 and argv[:2] == ["learning", "benchmark"]:
        from openkyrozen.learning.benchmark import main as benchmark_main
        benchmark_main(argv[2:])
        return

    args = self._parse_cli_args(argv)
    try:
        context = self.configure_launch_context(
            project_path=args.project,
            global_mode=args.global_mode,
        )
    except ValueError as exc:
        self._cli_parser().error(str(exc))
    self.bind_interaction_scope("surface:cli")

    if args.init:
        self.console.print(f"[{self._ACCENT}]OpenKyrozen initialisation[/{self._ACCENT}]")
        self.console.print(f"[{self._MUTED}]{context.describe()}[/{self._MUTED}]")
        try:
            import openai  # noqa: F401
        except ImportError:
            self.console.print(f"[{self._ERROR}]openai not installed. Run: pip install -r requirements.txt[/{self._ERROR}]")
            sys.exit(1)
        self._prompt_and_init_deepseek()
        self._load_project_files_into_memory()
        if self.llm_provider is not None:
            self.console.print(f"[{self._SUCCESS}]{self._provider_config.provider.title()} API key configured and saved to ~/.kyrozen_config.json[/{self._SUCCESS}]")
        next_command = "kyrozen" if context.is_global else f"kyrozen --project {context.active_root}"
        self.console.print(f"[{self._SUCCESS}]Initialisation finished. Run `{next_command}` to start the agent.[/{self._SUCCESS}]")
        sys.exit(0)

    # Resolve the effective configuration before rendering any provider status.
    # The same object is passed to initialization so the banner cannot report a
    # stale provider or model while startup is still selecting credentials.
    startup_config = self.detect_provider()
    self._provider_config = startup_config

    # ASCII-art banner, 41 chars wide — fits 60-col terminals
    self._print_banner(startup_config)
    self.console.print(f"[{self._MUTED}]{context.describe()}[/{self._MUTED}]")
    provider_name = startup_config.provider.title()
    model_name = startup_config.model_main or startup_config.model_simple
    self.console.print(f"[{self._ACCENT}]Kyrozen[/{self._ACCENT}] [{self._MUTED}]{self._DOT} Provider: {provider_name} {self._DOT} Model: {model_name}[/{self._MUTED}]")
    self.console.print(f"[{self._MUTED}]Chat:[/{self._MUTED}] [{self._ACCENT_DIM}] /mode /ask /plan /question /agent /system-one /fast /decision-assist /graph /github /skills /ponytail /provider /model /custom-provider /api_key /learn /update[/{self._ACCENT_DIM}]")

    # Compact self-learning summary
    enabled_count = sum(1 for v in self._SELF_LEARNING_FLAGS.values() if v)
    total_count = len(self._SELF_LEARNING_FLAGS)
    self.console.print(f"[{self._MUTED}]Self-learning:[/{self._MUTED}] [{self._ACCENT_DIM}]{enabled_count}/{total_count} features active (toggle with /self-learning)[/{self._ACCENT_DIM}]")
    self.console.print(f"[{self._MUTED}]Memory:[/{self._MUTED}] [{self._ACCENT_DIM}]SQLite v2 + rebuildable vector index — ask me what I remember[/{self._ACCENT_DIM}]")
    self.console.print(f"[{self._MUTED}]Cost:[/{self._MUTED}] [{self._ACCENT_DIM}]{get_cost_summary(
        store=self.memory_bank.store, user_id=self.memory_bank.user_id,
        workspace_id=self.memory_bank.workspace_id)}[/{self._ACCENT_DIM}]")
    # Horizontal rule
    self.console.print(f"[{self._ACCENT_DIM}]{self._BOX_H * 50}[/{self._ACCENT_DIM}]")

    self._prompt_and_init_deepseek(config=startup_config)
    if fast_mode.jev_key():
        model_info = fast_mode.jev_model_info(force=True)
        self.console.print(f"[{self._MUTED}]System One Jev: {model_info.get('alias', 'jev-latest')} "
                      f"({model_info.get('release_date') or 'release unknown'}; {model_info.get('health', 'unknown')})[/{self._MUTED}]")
    if self.llm_provider is None:
        self.console.print(f"[{self._ERROR}]Cannot start without an API key.[/{self._ERROR}]")
        sys.exit(1)
    self._plugin_runtime_for_surface().load_once()
    task_results = self._run_recovered_tasks()
    for item in task_results:
        self.console.print(f"[{self._SUCCESS}]Durable task {item['task']['id']}: {item['status']}[/{self._SUCCESS}]")
    # Build the private code graph in the background; the last valid graph stays usable.
    self._load_project_files_into_memory()
    self.console.print(f"[{self._MUTED}]Project graph indexing started in private state.[/{self._MUTED}]")

    # Hand learning off to a detached process so it survives CLI exit.  Keep
    # the in-process loop only as a safe fallback when process creation fails.
    if not self._ensure_detached_learning_worker():
        threading.Thread(target=self._background_learning_loop, daemon=True).start()

    while True:
        try:
            user_input = self.console.input(f"[bold {self._ACCENT}]You:[/bold {self._ACCENT}] ").strip()
        except (EOFError, KeyboardInterrupt):
            self.console.print(f"\n[{self._ERROR}]Goodbye.[/{self._ERROR}]")
            sys.exit(0)

        if not user_input:
            continue

        _is_auto_continue = False

        inline = split_inline_command(user_input)
        if inline:
            user_input, command = inline
            self._apply_inline_command(command)

        interaction_before = self.interaction_envelope(user_input)
        # A clarification answer or plan revision continues the same logical
        # task and must not erase its durable checklist.
        if (not _is_auto_continue and not user_input.startswith("/")
                and not interaction_before["pending_question"]
                and not interaction_before["pending_plan"]):
            self.tasks.clear()
            self._clear_tasks_panel()

        user_input = self._handle_cli_command(user_input, interaction_before)
        if user_input is not None:
            self._run_cli_chat(user_input, interaction_before)
