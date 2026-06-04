"""
Kyrozen: self-learning AI Agent powered by DeepSeek API + tools.
(Run `python main.py` to launch the agent)
"""

import json
import os
import re
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich import print as rprint

from memory import MemoryBank
from tools import AVAILABLE_TOOLS

console = Console()


# ---- Constants (optimized for DeepSeek) ----
MODEL_NAME = "deepseek-chat"  # fallback, actual model set by DEEPSEEK_MODEL env
SHORT_TERM_CAP = 16  # larger context window for DeepSeek
MAX_TOOL_RETRIES = 3

def _prompt_and_init_deepseek() -> None:
    """If DEEPSEEK_API_KEY is not in the environment, ask the user interactively."""
    global deepseek_client, DEEPSEEK_MODEL
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        key = input("\nDeepSeek API key not set. Enter your API key: ").strip()
        if not key:
            print("No API key entered – use /quit to exit.")
            deepseek_client = None
            DEEPSEEK_MODEL = "deepseek-chat"
            return
        os.environ["DEEPSEEK_API_KEY"] = key
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_client = OpenAI(api_key=key, base_url=base_url)


# ---- DeepSeek API support ----
DEEPSEEK_MODEL: str = "deepseek-chat"
deepseek_client = None


def _load_project_files_into_memory() -> None:
    """Read project Python files and store their content in the memory bank."""
    project_root = Path(__file__).parent.resolve()
    for py_file in project_root.rglob("*.py"):
        # Skip __pycache__ directories
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            # Store as "FILE: path" with content code block
            log_text = f"FILE: {py_file.relative_to(project_root)}\n```python\n{content}\n```"
            memory_bank.add_log(log_text)
        except Exception as e:
            print(f"Could not read {py_file}: {e}")


def _build_tools_list() -> str:
    """Build tools list for system prompt: name + docstring."""
    lines = []
    for name, fn in AVAILABLE_TOOLS.items():
        doc = getattr(fn, "__doc__", None) or ""
        desc = doc.strip().replace("\n", " ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


TOOLS_LIST = _build_tools_list()


def _system_prompt(tools_list: str) -> str:
    """Full system prompt optimized for DeepSeek models."""
    return f"""You are Kyrozen, an intelligent, self‑learning AI assistant. You have access to tools and can learn from project files and past conversations to improve your knowledge over time.

## Available Tools:
{tools_list}

## How to use a tool:
When you need to perform an action, you must output a Thought followed by an Action in JSON format enclosed in triple backticks.

**Format:**
Thought: (explain your reasoning)
Action:
```json
{{
  "action": "tool_name",
  "args": "arguments"
}}
```

If no action is required, respond naturally in plain text.

You are not limited to coding tasks; you can assist with any topic. Use the tools when appropriate, but feel free to engage in general conversation. Always try to provide thorough, helpful answers.

You have stored knowledge of the project's code files as well as previous conversations. Use that knowledge to improve your answers and learn over time.

You can also teach yourself new skills. If you want to create a new tool, output a **DefineTool:** block with a JSON definition. For example:

DefineTool:
```json
{{
  "name": "example_tool",
  "description": "what it does",
  "code": "def example_tool(args):\\n    return 'result'"
}}
```

After such definition the new tool will be available for future use.
"""


# ---- Shared state ----
memory_bank = MemoryBank()
short_term_memory: list[dict[str, str]] = [
    {"role": "user", "content": "Hello, are you ready to help me?"},
    {"role": "assistant", "content": "Yes! I can use tools like search_web and write_file. How can I help?"},
]


def _build_messages(user_input: str) -> list[dict[str, str]]:
    """Build message list: system (minimal) + optional memory + last N turns + user."""
    messages: list[dict[str, str]] = []

    # 1. Single minimal system prompt (no file I/O)
    messages.append({"role": "system", "content": _system_prompt(TOOLS_LIST)})

    # 2. Optional: one short memory hint if we have RAG results (saves tokens)
    recalled = memory_bank.recall(user_input, n_results=4)
    if recalled:
        memory_block = "Relevant past context:\n" + "\n".join(recalled[:2])
        messages.append({"role": "system", "content": memory_block})

    # 3. Last N turns
    for msg in short_term_memory[-SHORT_TERM_CAP * 2 :]:
        messages.append(msg)

    # 4. Current user input
    messages.append({"role": "user", "content": user_input})

    return messages


def parse_json_from_response(text: str) -> dict | None:
    """
    Extract tool-call JSON. Prefer 'Action: ```json ... ```' then any ```json ... ``` block.
    """
    text = (text or "").strip()
    # Prefer pattern: Action: ```json ... ```
    for pattern in (
        r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```",
        r"```(?:json)?\s*([\s\S]*?)\s*```",
    ):
        code_match = re.search(pattern, text)
        if code_match:
            raw = code_match.group(1).strip()
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and data.get("action") in AVAILABLE_TOOLS:
                    return data
            except json.JSONDecodeError:
                continue
    return None


def _run_tool(action: str, args: str) -> str:
    """Execute one tool; return result string."""
    fn = AVAILABLE_TOOLS.get(action)
    if not fn:
        return f"Error: unknown tool '{action}'"
    try:
        return str(fn(args))
    except Exception as e:
        return f"Error: {e}"


def _attempt_define_tool(text: str) -> bool:
    """If the LLM output contains a DefineTool block, parse it, create the function and add it to AVAILABLE_TOOLS."""
    pattern = r"DefineTool:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text)
    if not match:
        return False
    raw = match.group(1).strip()
    try:
        definition = json.loads(raw)
    except json.JSONDecodeError:
        return False

    name = definition.get("name", "").strip()
    description = definition.get("description", "").strip()
    code = definition.get("code", "").strip()

    if not name or not code:
        return False

    # Create a restricted namespace for exec
    local_vars: dict = {}
    try:
        exec(code, {"__builtins__": __builtins__}, local_vars)
    except Exception:
        return False

    # The function bearing the same name must have been defined
    if name not in local_vars:
        return False

    new_tool = local_vars[name] = local_vars.pop(name)
    if not callable(new_tool):
        return False

    # Add to the global tool registry
    AVAILABLE_TOOLS[name] = new_tool

    # Also store the definition in long‑term memory for future recall
    memory_bank.add_log(
        f"New tool defined: {name}\n"
        f"Description: {description}\n"
        f"Code:\n```python\n{code}\n```"
    )

    print(f"[DefineTool] Added new tool '{name}' to AVAILABLE_TOOLS.")
    return True


def _get_llm_response(messages: list[dict[str, str]]) -> str:
    """Call DeepSeek API; return assistant content."""
    if deepseek_client is None:
        return "[DeepSeek Error] DEEPSEEK_API_KEY environment variable not set"
    # Use DeepSeek API (OpenAI compatible)
    try:
        response = deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=messages,
        )
        text = response.choices[0].message.content or ""
        return text.strip()
    except Exception as e:
        return f"[DeepSeek Error] {e}"


def _chat_turn(user_input: str) -> str:
    """One user turn: build context, get LLM reply, handle tool call and retry if empty."""
    messages = _build_messages(user_input)
    response_text = _get_llm_response(messages).strip()

    # Retry if empty response
    if not response_text or not response_text.strip():
        messages.append({
            "role": "user",
            "content": "System: You returned nothing. Please output your Thought and JSON Action now.",
        })
        response_text = _get_llm_response(messages).strip()

    # Attempt to define a new tool if present
    if _attempt_define_tool(response_text):
        return "New tool defined. It will be available for future interactions."

    for attempt in range(MAX_TOOL_RETRIES + 1):
        tool_call = parse_json_from_response(response_text)
        if not tool_call:
            return response_text

        action = tool_call.get("action", "")
        args = tool_call.get("args", "") if isinstance(tool_call.get("args"), str) else str(tool_call.get("args", ""))

        result = _run_tool(action, args)

        if result.strip().lower().startswith("error") and attempt < MAX_TOOL_RETRIES:
            retry_messages = _build_messages(user_input)
            retry_messages.append({
                "role": "user",
                "content": f"Tool failed. Result: {result}. Try again with different action/args (output Thought then JSON).",
            })
            response_text = _get_llm_response(retry_messages).strip()
            continue

        tool_feedback = (
            f"System: Tool executed. Result: {result}. "
            "Summarize what you did for the user."
        )
        follow_up = _build_messages(user_input)
        follow_up.append({"role": "assistant", "content": response_text})
        follow_up.append({"role": "user", "content": tool_feedback})
        response_text = _get_llm_response(follow_up).strip() or result
        break

    return response_text


def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def main() -> None:
    banner_text = r"""
   ___   ___   ___   ___    _   _  __   ___
  / _ \ | _ \ / _ \ | _ \  | \ | | \ \ / / _ \
 | | | ||  _/ | | | ||  _/  |  \| |  \ V /| | | |
 | |_| || |   | |_| || |    | |\  |   | | | |_| |
  \___/ |_|    \___/ |_|    |_| \_|   |_|  \___/
"""
    console.print(Panel.fit(banner_text, title="OPENKYROZEN", subtitle="self‑learning AI agent", border_style="cyan"))
    console.print(f"Kyrozen (DeepSeek + Tools). Model: {MODEL_NAME}", style="cyan")
    console.print("Commands: /quit or /exit to exit, /save to long‑term memory, /learn to reload project files.\n", style="yellow")

    _prompt_and_init_deepseek()
    if deepseek_client is None:
        console.print("Cannot start without an API key.", style="red")
        sys.exit(1)
    _load_project_files_into_memory()

    while True:
        try:
            user_input = input("[bold cyan]You: [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]Goodbye.[/red]")
            sys.exit(0)

        if not user_input:
            continue
        if user_input.lower() in ("/quit", "/exit"):
            console.print("[red]Goodbye.[/red]")
            break
        if user_input.lower() == "/save":
            memory_bank.add_log(
                "Conversation: " + "; ".join(
                    m.get("content", "")[:200] for m in short_term_memory[-6:] if m.get("content")
                )
            )
            console.print("[green]Saved to long‑term memory.[/green]")
            continue
        if user_input.lower() == "/learn":
            _load_project_files_into_memory()
            console.print("[green]Project files re‑learned and stored in memory.[/green]")
            continue

        reply = _chat_turn(user_input)

        if len(reply.strip()) < 5:
            console.print("[red][Error] Received empty response from LLM[/red]")
            continue

        short_term_memory.append({"role": "user", "content": user_input})
        short_term_memory.append({"role": "assistant", "content": reply})
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        # Render markdown in a panel
        console.print(Panel(Markdown(reply), title="Agent", border_style="green"))
        print()  # spacing


if __name__ == "__main__":
    main()
