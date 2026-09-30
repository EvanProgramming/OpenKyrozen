from __future__ import annotations

import re
import time
from openkyrozen.persistence.models import stable_hash

def _maybe_trigger_reflection_after_complex_task(self, num_tool_calls: int) -> None:
    """Trigger reflection after a multi‑tool task ends (not idle)."""
    now = time.time()
    if now - self._last_task_end < 60:
        return  # at most once per minute
    self._last_task_end = now
    # Only reflect on tasks that required several tool calls
    if num_tool_calls < 2:
        return
    recent = self.memory_bank.get_recent(20)
    if not recent:
        return
    recent_tokens = sum(entry.get("tokens", 0) for entry in self._turn_cost_log[-5:])
    recent_time = sum(entry.get("time", 0) for entry in self._turn_cost_log[-5:])
    cost_summary = f"Recent token count: {recent_tokens}, recent runtime: {recent_time:.1f}s" if self._turn_cost_log else ""
    reflect_prompt = (
        "You are Kyrozen's reflection module. The last task used {num_tool_calls} tool calls. "
        "Analyse whether a more efficient approach exists. "
        f"{cost_summary}\n"
        "Output the optimised strategy as a numbered list. If nothing to improve, output '—'.\n\n"
        + "\n".join(recent[-10:])
    )
    try:
        messages = [{"role": "system", "content": reflect_prompt}]
        answer = (self._learning_model_response(messages, feature="post_task_reflection") or "").strip()
        if answer and answer not in ("—", ""):
            self.memory_bank.add_log(f"REFLECTION:\n{answer}")
    except Exception:
        pass


def _maybe_trigger_reflection(self) -> None:
    """If at least 3 messages have been exchanged since last reflection and idle, reflect."""
    now = time.time()
    if now - self._last_task_end < 300:
        return  # not idle long enough
    self._last_task_end = now
    recent = self.memory_bank.get_recent(20)
    if not recent:
        return
    # Compute token cost of recent turns
    recent_tokens = sum(
        entry.get("tokens", 0) for entry in self._turn_cost_log[-5:]
    )
    recent_time = sum(
        entry.get("time", 0) for entry in self._turn_cost_log[-5:]
    )
    cost_summary = f"Recent token count: {recent_tokens}, recent runtime: {recent_time:.1f}s" if self._turn_cost_log else ""
    reflect_prompt = (
        "You are Kyrozen's reflection module. Read the recent interactions and find a non‑trivial task "
        "that took several steps. Analyse whether a more efficient approach exists. "
        f"{cost_summary}\n"
        "Output the optimised strategy as a numbered list. If nothing to improve, output '—'.\n\n"
        + "\n".join(recent[-10:])
    )
    try:
        messages = [{"role": "system", "content": reflect_prompt}]
        answer = (self._learning_model_response(messages, feature="idle_reflection") or "").strip()
        if answer and answer not in ("—", ""):
            self.memory_bank.add_log(f"REFLECTION:\n{answer}")
    except Exception:
        pass


def _maybe_strategy_distillation(self) -> None:
    """If recent turns consumed many tokens, distill an efficient strategy."""
    if len(self._turn_cost_log) < 3:
        return
    recent_total = sum(entry.get("tokens", 0) for entry in self._turn_cost_log[-5:])
    if recent_total < 5000:
        return

    # Gather recent conversation context for analysis
    recent_logs = self.memory_bank.get_recent(30)
    if not recent_logs:
        return
    # Filter out system‑internal entries
    user_logs = [r for r in recent_logs if r and not r.startswith(("FILE:", "FACT:", "LEARNED:", "SKILL:", "DEBUG:", "TOOL_REVIEW:"))]
    if len(user_logs) < 5:
        return

    distill_prompt = (
        "You are Kyrozen's strategy distillation module. Recent turns consumed "
        f"{recent_total} tokens. Analyse the conversation patterns below and "
        "distill 1‑3 concise strategies the agent should adopt to work more "
        "efficiently (fewer tool calls, less token waste, faster execution).\n\n"
        "Output each strategy on a new line prefixed with 'STRATEGY:'.\n"
        "If no clear improvement, output '—'.\n\n"
        + "\n".join(user_logs[-15:])
    )
    try:
        messages = [{"role": "system", "content": distill_prompt}]
        answer = (self._learning_model_response(messages, feature="strategy_distillation") or "").strip()
        if answer and answer not in ("—", ""):
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("STRATEGY:"):
                    self.memory_bank.add_log(f"STRATEGY: {line}")
    except Exception:
        pass


def _targeted_inquiry(self) -> None:
    """Scan project files for undocumented functions and infer their purpose via LLM.
    This is self-learning: the agent analyses code itself, never asks the user."""
    now = time.time()
    if now - self._last_user_interaction < 300:
        return
    if now - self._last_inquiry_time < 600:  # once per 10 min
        return
    self._last_inquiry_time = now

    project_root = self._get_workspace_root()
    for py_file in project_root.rglob("*.py"):
        if "__pycache__" in str(py_file) or py_file.name.startswith("test_"):
            continue
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        # Find function definitions without docstrings
        for match in re.finditer(
            r"def\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*\S+\s*)?:\s*\n(\s+)(\S.*)",
            content
        ):
            func_name = match.group(1)
            func_params = match.group(2)
            indent = match.group(3)
            first_line = match.group(4).strip()

            inquiry_key = f"{py_file}:{func_name}:{stable_hash(content[match.start():match.end()]) if 'stable_hash' in vars(self) else hash(content[match.start():match.end()])}"
            if inquiry_key in self._inquired_functions:
                continue
            self._inquired_functions.add(inquiry_key)

            # Skip if it already has a docstring
            if first_line.startswith('"""') or first_line.startswith("'''"):
                continue

            # Extract function body (up to ~30 lines for context)
            func_start = match.start()
            body_start = content.index("\n", match.end(3)) + 1
            lines = content[body_start:].split("\n")
            body_lines = []
            for line in lines:
                if line.strip() and not line.startswith(indent):
                    break
                body_lines.append(line)
                if len(body_lines) >= 30:
                    break
            body_text = "\n".join(body_lines)

            # Use LLM to infer what this function does
            analyze_prompt = (
                "You are Kyrozen's code analysis module. Examine this Python "
                f"function from {py_file.name} and infer its purpose.\n\n"
                f"```python\ndef {func_name}({func_params}):\n{body_text}\n```\n\n"
                "Output a single line starting with 'PURPOSE: ' followed by "
                "a concise description of what this function does, its inputs, "
                "and its outputs. If unclear, output 'PURPOSE: unclear'."
            )
            try:
                messages = [{"role": "system", "content": analyze_prompt}]
                answer = (self._learning_model_response(messages, feature="targeted_inquiry") or "").strip()
                if answer.startswith("PURPOSE: "):
                    purpose = answer[len("PURPOSE: "):].strip()
                    if purpose.lower() != "unclear":
                        self.memory_bank.add_log(
                            f"CODE_DOC: Function '{func_name}' in {py_file.name} "
                            f"({func_params}) — {purpose}"
                        )
            except Exception as exc:
                self.memory_bank.store.append_event(
                    "learning.inquiry_failed", {"path": str(py_file), "function": func_name, "error": str(exc)[:500]},
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id,
                )
            return  # one function per cycle, with a durable cursor
