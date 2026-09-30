from __future__ import annotations

import sys
from rich.markdown import Markdown
from rich.panel import Panel
from openkyrozen.agent.modes import is_plan_acceptance

def _run_cli_chat(self, user_input, interaction_before):

    # Prompt injection check
    sanitized, flagged = self._sanitize_input(user_input)
    if flagged:
        self.console.print(f"[{self._WARNING}]Prompt injection detected and filtered.[/{self._WARNING}]")

    try:
        reply = self.chat(self.current_session, sanitized, clear_tasks=not bool(
            interaction_before["pending_question"] or interaction_before["pending_plan"]
            or is_plan_acceptance(sanitized)
        ))
    except Exception as e:
        import traceback as _tb
        self.console.print(f"[{self._ERROR}]=== EXCEPTION IN _chat_turn ===[/{self._ERROR}]")
        _tb.print_exc(file=sys.stderr)
        self.console.print(f"[{self._ERROR}]Exception type: {type(e).__name__}[/{self._ERROR}]")
        self.console.print(f"[{self._ERROR}]Exception message: {e}[/{self._ERROR}]")
        reply = f"Error: {e}"

    if len(reply.strip()) < 5:
        self.console.print(f"[{self._ERROR}][Error] Received empty response from LLM[/{self._ERROR}]")
        return

    self.short_term_memory.append({"role": "user", "content": user_input})
    cleaned_reply = self._remove_task_blocks(reply)
    self.short_term_memory.append({"role": "assistant", "content": cleaned_reply})
    self.memory_bank.add_log(f"User: {user_input}\nAssistant: {self._clean_final_response(reply)}")

    thinking, answer = self._split_reply(reply)

    if thinking:
        self.console.print(Panel(thinking, title="Thinking", border_style=self._ACCENT_DIM))
    if answer:
        self.console.print(Panel(Markdown(answer), title="Kyrozen", border_style=self._ACCENT, title_align="left"))
    else:
        self.console.print(Panel(Markdown(answer or "(no content)"), title="Kyrozen", border_style=self._ACCENT, title_align="left"))
    print()

    # Show tasks panel if any tasks exist
    if self.tasks.tasks:
        self._update_tasks_panel()
        print()

    # auto‑continue: process further Action blocks without user input
    _is_auto_continue = True
    _auto_continue_limit = 5
    _original_user_input = user_input
    while _auto_continue_limit > 0 and not _original_user_input.startswith("/"):
        potential_actions = self._collect_tool_calls(reply)
        if not potential_actions:
            break
        # treat reply as new user input for the next LLM call
        user_input = reply
        _auto_continue_limit -= 1
        try:
            reply = self.chat(self.current_session, user_input)
        except Exception as e:
            self.console.print(f"[{self._ERROR}]Error: {e}[/{self._ERROR}]")
            reply = f"Error: {e}"
            break
        if len(reply.strip()) < 5:
            self.console.print(f"[{self._ERROR}][Error] Received empty response from LLM[/{self._ERROR}]")
            break
        self.short_term_memory.append({"role": "user", "content": user_input})
        self.short_term_memory.append({"role": "assistant", "content": reply})
        self.memory_bank.add_log(f"User: {user_input}\nAssistant: {self._clean_final_response(reply)}")
        thinking, answer = self._split_reply(reply)
        if thinking:
            self.console.print(Panel(thinking, title="Thinking", border_style=self._ACCENT_DIM))
        if answer:
            self.console.print(Panel(Markdown(answer), title="Kyrozen", border_style=self._ACCENT, title_align="left"))
        else:
            self.console.print(Panel(Markdown(answer or "(no content)"), title="Kyrozen", border_style=self._ACCENT, title_align="left"))
        print()
        if self.tasks.tasks:
            self._update_tasks_panel()
            print()

    _is_auto_continue = False
