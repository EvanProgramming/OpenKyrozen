from __future__ import annotations

import os as _os
import subprocess
import sys
import threading
import time
from rich.panel import Panel
from rich.markup import escape as rich_escape
from openkyrozen.tasks.engine import canonical_status
from openkyrozen.agent.modes import InteractionError
import openkyrozen.routing.system_one as fast_mode
from openkyrozen.workspace.context import LaunchContext
from openkyrozen.agent.types import ContextOverflowError, ContextTooLargeError


def _terminal_supports_unicode(self) -> bool:
    """Return True if the terminal can render Unicode box-drawing and special chars."""
    if not self._IS_WINDOWS:
        return True
    # Windows Terminal, VS Code terminal, and ConEmu all set WT_SESSION
    if _os.environ.get("WT_SESSION") or _os.environ.get("TERM_PROGRAM") == "vscode":
        return True
    # PowerShell 6+ generally supports UTF-8
    if "pwsh" in _os.environ.get("TERM_PROGRAM", "").lower():
        return True
    # Check codepage — 65001 is UTF-8
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleOutputCP() == 65001:
            return True
    except Exception:
        pass
    return False


def _workspace_info(self) -> str:
    """Return the active workspace and the selected launch mode."""
    root = self._get_workspace_root()
    context = getattr(self, "_launch_context", None)
    if isinstance(context, LaunchContext):
        return (
            f"{context.describe()}\n\n"
            "Relative file operations resolve from the active workspace.\n"
            "Do not ask the user for a local path unless an explicit external repository is intended."
        )
    return (
        f"The configured workspace is `{root}`.\n\n"
        "Relative file operations resolve from this workspace.\n"
        "Do not ask the user for a local path unless an explicit external repository is intended."
    )


def _tasks_panel_height(self) -> int:
    if not self.tasks.tasks:
        return 0
    return min(len(self.tasks.tasks) + 4, 12)


def _task_status_counts(self) -> dict[str, int]:
    """Return durable task counts in a stable order for every surface."""
    counts = {status: 0 for status in self._TASK_STATUS_ORDER}
    for task in self.tasks.tasks:
        counts[canonical_status(task.get("status", "pending"))] += 1
    return counts


def _tasks_panel_content(self) -> str:
    """Build task panel as Rich-markup string (no raw ANSI)."""
    if not self.tasks.tasks:
        return ""
    total = len(self.tasks.tasks)
    counts = self._task_status_counts()
    done = counts["succeeded"]
    bar_w = 20
    filled = int(bar_w * done / max(total, 1))
    bar = self._BAR_FILL * filled + self._BAR_EMPTY * (bar_w - filled)
    lines = [f"[bold white on {self._ACCENT_BG}] {self._BOX_TL}{self._BOX_H}{self._BOX_H} TASKS [{bar}] {done}/{total} [/]"]
    status_icons = {
        "succeeded": self._CHECK, "failed": "!", "blocked": "⚠" if self._UNICODE_OK else "B",
        "cancelled": "×", "pending": self._CIRCLE, "running": self._HALF,
    }
    status_colors = {
        "succeeded": self._SUCCESS, "failed": self._ERROR, "blocked": self._WARNING,
        "cancelled": self._MUTED, "pending": self._WARNING, "running": self._ACCENT,
    }
    status_line = "  ".join(
        f"[{status_colors[status]}]{status_icons[status]} {status}={counts[status]}"
        f"[/{status_colors[status]}]"
        for status in self._TASK_STATUS_ORDER
    )
    lines.append(f"[white on {self._ACCENT_BG}] {self._BOX_V} {status_line} [/]")
    for i, t in enumerate(self.tasks.tasks):
        status = canonical_status(t["status"])
        icon = status_icons[status]
        color = status_colors[status]
        desc = t["description"][:55]
        lines.append(
            f"[white on {self._ACCENT_BG}] {self._BOX_V} [{color}]{icon}[/{color}] "
            f"[{self._MUTED}]{i}[/{self._MUTED}] {desc} [{color}]{status}[/{color}] [/]"
        )
    unfinished = counts["pending"] + counts["running"]
    attention = counts["failed"] + counts["blocked"] + counts["cancelled"]
    if unfinished:
        suffix = f"; {attention} require attention" if attention else ""
        lines.append(f"[bold white on {self._ACCENT_BG}] {self._BOX_BL}{self._BOX_H}{self._BOX_H} {unfinished} unfinished{suffix} — DO NOT STOP [/]")
    elif attention:
        lines.append(f"[bold white on {self._ACCENT_BG}] {self._BOX_BL}{self._BOX_H}{self._BOX_H} Tasks require attention: {attention} non-success [/]")
    else:
        lines.append(f"[bold white on {self._ACCENT_BG}] {self._BOX_BL}{self._BOX_H}{self._BOX_H} All tasks complete {self._CHECK} [/]")
    return "\n".join(lines)


def _update_tasks_panel(self) -> None:
    """Render task panel at current cursor position via Rich."""
    content = self._tasks_panel_content()
    if not content:
        return
    try:
        self.console.print(Panel(content, title="Tasks", border_style=self._ACCENT))
    except Exception:
        pass


def _clear_tasks_panel(self) -> None:
    """No‑op — panel is inline, cleared naturally by new output."""


def _apply_inline_command(self, command: str) -> bool:
    """Apply a command that modifies the following chat request."""
    parts = command.split()
    name = parts[0].lower() if parts else ""
    args = [part.lower() for part in parts[1:]]
    if name == "/ask":
        self.set_interaction_mode("ask")
        self.console.print("Interaction mode set to ask.")
    elif name == "/mode":
        try:
            self.console.print(f"Interaction mode set to {self.set_interaction_mode(args[0])['preference_mode']}.")
        except InteractionError as exc:
            self.console.print(f"Usage: /mode auto|ask|plan|agent ({exc})")
    elif name == "/plan":
        self.set_interaction_mode("plan")
        self.console.print("Interaction mode set to plan.")
    elif name == "/agent":
        pass
        self._agent_profile_mode = args[0] if args else self._agent_profile_mode
        if args:
            self._restore_user_preferences()
            self.console.print(f"Agent profile set to {self._agent_profile_mode}.")
        else:
            self.console.print(f"Agent profile: {self._agent_profile_mode}")
    elif name in {"/fast", "/system-one", "/system_one"}:
        if not args:
            state = fast_mode.decision_assist_state()
            backend_name = self.interaction_envelope()["system_one_backend"]
            backends = (state.get("calibration", {}).get("backends", {})
                        if isinstance(state.get("calibration"), dict) else {})
            policies = backends.get(backend_name, {}) if isinstance(backends, dict) else {}
            calibrated = any(bool(policy.get("validated"))
                             for backend_policies in policies.values() if isinstance(backend_policies, dict)
                             for policy in backend_policies.values() if isinstance(policy, dict))
            self.console.print(
                f"System One: {backend_name} · "
                f"Jev model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'}; {state.get('jev_health', 'unknown')}) · "
                f"calibration: {'available' if calibrated else 'not calibrated'} · "
                "Jev sends context to TypeSafe; local Kev-0.8B is less accurate"
            )
        else:
            try:
                key = None
                if args[0] == "jev" and not fast_mode.jev_key():
                    import getpass
                    key = getpass.getpass("Jev API key (paid TypeSafe calls): ")
                if args[0] == "kev":
                    self.console.print("Installing and starting local Kev-0.8B; waiting for a live check…")
                self.console.print(f"System One: {self.set_system_one_backend(args[0], api_key=key)['system_one_backend']}")
            except (InteractionError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                self.console.print(f"System One setup failed: {exc}")
    elif name in {"/decision-assist", "/assist"}:
        if not args:
            state = self.decision_assist_state()
            assist_policies = (state.get("calibration", {}).get("backends", {}).get(state["backend"], {})
                               if isinstance(state.get("calibration"), dict) else {})
            calibrated = sum(bool(policy.get("validated")) for policy in assist_policies.values()
                             if isinstance(policy, dict))
            self.console.print(
                f"Decision Assist: {state['backend']} · Jev configured: {state['jev_configured']} · "
                f"Kev ready: {state['kev_ready']} · private Kev consent: {state['kev_private_consent']} · "
                f"Jev model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'}; {state.get('jev_health', 'unknown')}) · "
                f"calibrated actions: {calibrated}"
            )
        else:
            try:
                backend = args[0]
                if backend == "revoke":
                    state = self.revoke_decision_assist_consent()
                else:
                    key = None
                    if backend == "jev" and not fast_mode.jev_key():
                        import getpass
                        key = getpass.getpass("Jev API key (paid TypeSafe calls): ")
                    consent = len(args) > 1 and args[1] in {"y", "yes", "consent", "allow"}
                    state = self.set_decision_assist(backend, private_consent=consent, api_key=key)
                self.console.print(
                    f"Decision Assist: {state['backend']} · Kev private consent: "
                    f"{state['kev_private_consent']} · Jev model: {state.get('jev_model_alias', 'jev-latest')}"
                )
            except (InteractionError, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.console.print(f"Decision Assist setup failed: {exc}")
    elif name == "/ponytail":
        if not args:
            self.console.print(f"Ponytail: {self._ponytail_level}")
        else:
            try:
                self.console.print(f"Ponytail: {self.set_ponytail_level(args[0])}")
            except ValueError as exc:
                self.console.print(f"Usage: /ponytail off|lite|full|ultra ({exc})")
    else:
        return False
    return True


def _spinner_worker(self, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        for frame in self._SPINNER_FRAMES:
            if stop_event.is_set():
                break
            sys.stdout.write("\r" + frame + " ")
            sys.stdout.flush()
            time.sleep(0.25)


def _call_llm_with_spinner(self, messages: list[dict], model: str | None = None) -> str:
    streaming = callable(self._stream_event_callback.get())
    try:
        self._prepare_context_for_call(messages, model)
    except ContextTooLargeError as exc:
        return f"[LLM Error] {exc}"

    def call_once() -> str:
        if streaming:
            return self._get_llm_response(
                messages, model=model, stream=True,
                on_chunk=lambda chunk: self._emit_stream_event({"event": "content", "chunk": str(chunk)}),
                on_stream_end=lambda: self._emit_stream_event({"event": "model_complete"}),
            )
        return self._get_llm_response(messages, model=model)

    def call_with_one_overflow_recovery() -> str:
        try:
            return call_once()
        except ContextOverflowError as exc:
            if not self._recover_context_after_overflow(messages, model):
                return f"[LLM Error] {exc}"
            try:
                return call_once()
            except ContextOverflowError as retry_exc:
                return f"[LLM Error] {retry_exc}"

    if streaming:
        return call_with_one_overflow_recovery()
    self._SPINNER_STOP.clear()
    self._SPINNER_THREAD = threading.Thread(target=self._spinner_worker, args=(self._SPINNER_STOP,), daemon=True)
    self._SPINNER_THREAD.start()
    try:
        result = call_with_one_overflow_recovery()
    finally:
        self._SPINNER_STOP.set()
        if self._SPINNER_THREAD:
            self._SPINNER_THREAD.join(timeout=2)
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
    return result


def _render_tool_result(self, action: str, result: str) -> None:
    self.console.print(Panel(rich_escape(result), title=f"Tool: {action}", border_style=self._ACCENT_DIM, title_align="left"))


def _render_warning(self, message: str) -> None:
    self.console.print(f"[{self._WARNING}]{message}[/{self._WARNING}]")
