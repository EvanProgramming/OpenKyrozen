from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from rich.panel import Panel
from openkyrozen.tasks.engine import TaskWorker
from openkyrozen.providers import ProviderConfig, PROVIDER_ENV_VARS, PROVIDER_FALLBACKS

def _show_self_learning_menu(self) -> None:
    """Display an interactive menu to toggle self-learning features."""
    flag_names = [
        (name, self._LEARNING_FEATURE_REGISTRY[name]["description"])
        for name in self._LEARNING_FEATURE_ORDER
    ]
    while True:
        self.console.print(f"\n[bold {self._ACCENT}]═══ Self‑Learning Features ═══[/bold {self._ACCENT}]")
        runtime = self.learning_runtime()
        self.console.print(f"[{self._MUTED}]Learning: {runtime['mode']} / {runtime['status']} / {runtime['model'] or 'no model'} — {runtime['detail']}[/{self._MUTED}]")
        self.console.print(f"[{self._MUTED}]Enter a number, 'mode local' (free Qwen2.5 setup), 'mode remote', or 'done'.[/{self._MUTED}]\n")
        feature_status = {item["name"]: item for item in self.learning_feature_status()}
        for i, (key, desc) in enumerate(flag_names):
            enabled = self._SELF_LEARNING_FLAGS[key]
            icon = f"[{self._SUCCESS}]●[/{self._SUCCESS}]" if enabled else f"[{self._MUTED}]○[/{self._MUTED}]"
            state = feature_status[key]
            self.console.print(f"  [{self._MUTED}]{i+1}.[/{self._MUTED}] {icon} {desc} [{self._MUTED}]({state['status']} · {state['product_status']})[/{self._MUTED}]")
        self.console.print()
        choice = self.console.input("[bold cyan]Toggle (number), mode, or 'done': [/bold cyan]").strip().lower()
        if choice == "done":
            self.console.print(f"[{self._SUCCESS}]Self‑learning settings updated.[/{self._SUCCESS}]")
            break
        if choice.startswith("mode "):
            try:
                self.console.print(f"Learning policy: {self.set_learning_policy(choice.split(maxsplit=1)[1])}")
            except ValueError as exc:
                self.console.print(f"[{self._ERROR}]{exc}[/{self._ERROR}]")
            continue
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(flag_names):
                key = flag_names[idx][0]
                self._SELF_LEARNING_FLAGS[key] = not self._SELF_LEARNING_FLAGS[key]
                try:
                    self.memory_bank.store.set_learning_feature_flag(
                        key, self._SELF_LEARNING_FLAGS[key],
                        user_id=self.memory_bank.user_id,
                        workspace_id=self.memory_bank.workspace_id,
                    )
                    self._record_learning_event("learning.feature_toggled", {
                        "feature": key, "enabled": self._SELF_LEARNING_FLAGS[key],
                    })
                except Exception:
                    pass
                state = "enabled" if self._SELF_LEARNING_FLAGS[key] else "disabled"
                self.console.print(f"[{self._SUCCESS}]Toggled {flag_names[idx][1]} → {state}.[/{self._SUCCESS}]")
            else:
                self.console.print(f"[{self._ERROR}]Invalid number.[/{self._ERROR}]")
        except ValueError:
            self.console.print(f"[{self._ERROR}]Please enter a number or 'done'.[/{self._ERROR}]")


def _available_fallback_names(self, config: ProviderConfig | None) -> list[str]:
    """Return fallbacks that the runtime can configure without making a call."""
    if config is None:
        return []
    available: list[str] = []
    for provider_name in PROVIDER_FALLBACKS.get(config.provider, []):
        env_var = PROVIDER_ENV_VARS.get(provider_name, "")
        if provider_name == "ollama" or (env_var and os.environ.get(env_var, "").strip()):
            available.append(provider_name)
    return available


def _print_banner(self, config: ProviderConfig | None = None) -> None:
    """Print the banner using the provider configuration selected for startup."""
    config = config or self._provider_config
    provider_name = config.provider.title() if config else "Unknown"
    model_name = config.model_simple if config else "unresolved"
    fallback_names = self._available_fallback_names(config)
    fallback_status = ""
    if fallback_names:
        fallback_status = f"  {self._DOT}  Fallbacks available: {', '.join(name.title() for name in fallback_names)}"
    # Platform tag
    if self._IS_WINDOWS:
        platform_tag = "Windows"
    elif self._IS_MACOS:
        platform_tag = "macOS"
    elif self._IS_LINUX:
        platform_tag = "Linux"
    else:
        platform_tag = self._PLATFORM
    banner = (
        f"[bold white]OPEN[/bold white][bold {self._ACCENT}]KYROZEN[/bold {self._ACCENT}]\n"
        f"[{self._MUTED}]self{self._NBHYPHEN}learning AI agent  {self._DOT}  "
        f"{provider_name}  {self._DOT}  {model_name}{fallback_status}[/{self._MUTED}]\n"
        f"[{self._MUTED}]Platform: {platform_tag} {self._DOT} Python "
        f"{sys.version_info.major}.{sys.version_info.minor}[/{self._MUTED}]"
    )
    self.console.print(Panel(banner, border_style=self._ACCENT_DIM, padding=(1, 2), expand=False))


def _run_recovered_tasks(self, *, max_tasks: int = 20) -> list[dict[str, Any]]:
    """Recover pending CLI tasks and execute only the bounded safe queue."""
    recovered = self.tasks.recover()
    if recovered:
        self.console.print(f"[{self._MUTED}]Recovered {len(recovered)} durable task(s).[/{self._MUTED}]")
    return TaskWorker(self.tasks, self._execute_durable_task, max_tasks=max_tasks).run_until_idle(max_tasks=max_tasks)


def _cli_parser(self) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kyrozen",
        description="OpenKyrozen self-learning AI agent",
        epilog="Bare kyrozen uses ~/.kyrozen/workspace; use --project PATH for direct project operation.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--project", metavar="PATH",
        help="operate directly on this project directory",
    )
    mode.add_argument(
        "--global", dest="global_mode", action="store_true",
        help="use the persistent global workspace (the default)",
    )
    parser.add_argument("--init", action="store_true", help="configure a provider and initialise the active workspace")
    parser.add_argument("--version", action="version", version=f"OpenKyrozen {self.__version__}")
    return parser


def _parse_cli_args(self, argv: list[str] | None = None) -> argparse.Namespace:
    return self._cli_parser().parse_args(sys.argv[1:] if argv is None else argv)
