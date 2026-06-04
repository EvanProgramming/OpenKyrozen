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
CONFIG_PATH = os.path.expanduser("~/.kyrozen_config.json")

def _load_config_key() -> str | None:
    """Return the API key stored in config file, or None."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            key = data.get("api_key")
            if key and isinstance(key, str) and key.strip():
                os.environ["DEEPSEEK_API_KEY"] = key.strip()
                return key.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_config_key(key: str) -> None:
    """Persist the API key to the config file."""
    try:
        existing = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                existing = json.load(f)
        existing["api_key"] = key.strip()
        with open(CONFIG_PATH, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError:
        console.print("[yellow]Warning: could not save API key to config file.[/yellow]")


def _prompt_and_init_deepseek() -> None:
    """Load API key from config file or ask the user interactively."""
    global deepseek_client, DEEPSEEK_MODEL
    # First try from config
    key = _load_config_key()
    if not key:
        # Then try environment (may have been set external)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        key = input("\nDeepSeek API key not set. Enter your API key: ").strip()
        if not key:
            print("No API key entered – use /quit to exit.")
            deepseek_client = None
            DEEPSEEK_MODEL = "deepseek-chat"
            return
        # Save for future sessions
        _save_config_key(key)
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

## Current working directory
You are currently inside the project root directory of the repository the user is working in.  Relative paths (like "README.md" or "main.py") will be resolved correctly.  You can use `read_file`, `write_file`, `list_dir`, `find_files`, `run_cmd`, etc. **without needing the user to provide a path**.  Do **not** ask the user to supply a local path or a remote URL unless you intend to use the `analyze_remote_repo` tool to clone an external repository.

## How to use tools
When you need to perform an action, you **must** output a Thought followed by **exactly one** Action in JSON format enclosed in triple backticks.

**Format:**
Thought: (explain your reasoning)
Action:
```json
{{
  "action": "tool_name",
  "args": "arguments"
}}
```

If you need to run several tools in sequence, run **one per message** – the system will then ask you for the next step.

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

### Important reminder
When the user asks you to analyze the repository, start by running `list_dir('.')` to see the files.  Do **not** ask for a local path or remote URL – you are already in the correct directory.
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

    # 1b. Tell the assistant where it is
    messages.append({
        "role": "system",
        "content": f"You are currently working inside the directory:\n{os.getcwd()}\n\nYou can use relative paths like '.' or 'main.py' directly.  Do not ask the user for a local path or a remote URL unless you intend to clone an external repository."
    })

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
    Extract tool-call JSON.  Tries several patterns:
    1.  Action: ```json ... ```  (preferred)
    2.  Action:  (without backticks) followed by a JSON object on the next line(s)
    3.  Any ```json ... ``` block
    4.  Any JSON object that contains an "action" key (relaxed fallback)
    """
    text = (text or "").strip()
    # Pattern 1: Action: ```json ... ```
    for pattern in (
        r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```",
        r"Action:\s*(\{[\s\S]*?\})\s*(?:```|$)",
        r"```(?:json)?\s*([\s\S]*?)\s*```",
    ):
        for match in re.finditer(pattern, text):
            raw = match.group(1).strip()
            # Sometimes the raw still has stray ```` at the end; strip them
            raw = raw.rstrip("`").strip()
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and data.get("action") in AVAILABLE_TOOLS:
                    return data
            except json.JSONDecodeError:
                continue

    # Fallback: scan the whole text for any { ... } that might be valid JSON with "action"
    try:
        # Naive: find first { ... } pair
        start = text.find("{")
        if start != -1:
            end = text.rfind("}")
            if end != -1 and end > start:
                raw = text[start:end + 1]
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and data.get("action") in AVAILABLE_TOOLS:
                    return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def _extract_json_objects(text: str) -> list[dict]:
    """Return all JSON objects found in text, without requiring surrounding backticks."""
    objects: list[dict] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        for pos in range(start, len(text)):
            char = text[pos]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:pos + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    i = pos + 1
                    break
        else:
            # no matching closing brace found – move past the opening brace
            i = start + 1
    return objects


def _collect_tool_calls(text: str) -> list[dict]:
    """Return a list of tool‑call dicts found in the text (all Action blocks)."""
    calls: list[dict] = []

    # 1. Prefer the standard Action: ```json ... ``` pattern
    pattern = r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.finditer(pattern, text, re.DOTALL):
        raw = match.group(1).strip()
        raw = raw.rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and data.get("action") in AVAILABLE_TOOLS:
            calls.append(data)

    # 2. Also look for any JSON object that looks like a tool call (no backticks)
    for obj in _extract_json_objects(text):
        if isinstance(obj, dict) and "action" in obj and obj.get("action") in AVAILABLE_TOOLS:
            # Avoid duplicates (if a tool already captured via pattern 1)
            if obj not in calls:
                calls.append(obj)

    return calls


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


def _is_tool_error(result: str) -> bool:
    """Return True if the tool result indicates a failure."""
    low = result.strip().lower()
    return (
        low.startswith("error") or
        low.startswith("exit code") or
        "no such file" in low or
        "syntax error" in low or
        "not found" in low
    )


def _chat_turn(user_input: str) -> str:
    """One user turn: build context, get LLM reply, execute tool calls
    with automatic retries on failure."""

    MAX_RETRIES = 3
    messages = _build_messages(user_input)
    response_text = _get_llm_response(messages).strip()

    # Retry if empty response
    if not response_text or not response_text.strip():
        messages.append({
            "role": "user",
            "content": "System: You returned nothing. Please output your Thought and JSON Action now.",
        })
        response_text = _get_llm_response(messages).strip()

    # Check for new tool definitions before anything else
    if _attempt_define_tool(response_text):
        return "New tool defined. It will be available for future interactions."

    # Collect any tool calls the assistant made
    tool_calls = _collect_tool_calls(response_text)
    if not tool_calls:
        # No tools needed – return the plain answer
        return response_text

    # Execute all tools
    results: list[str] = []
    for tc in tool_calls:
        action = tc.get("action", "")
        args = tc.get("args", "")
        if not isinstance(args, str):
            args = str(args)
        result = _run_tool(action, args)
        print(f"[Tool] {action}({args!r}) → {result[:200]}...")
        results.append(f"- `{action}({args!r})` returned:\n{result[:2000]}")
    tool_results_text = "\n".join(results)

    # Check if any tool failed
    all_ok = not any(_is_tool_error(r) for r in results)

    if not all_ok:
        # Retry loop
        for attempt in range(MAX_RETRIES):
            # Feed failure back to LLM
            messages.append({
                "role": "user",
                "content": (
                    f"The previous tool attempt result(s):\n{tool_results_text}\n\n"
                    "The tool(s) failed. Please **try a different approach** "
                    "(e.g., quote the path, escape parentheses, or use a different command). "
                    "Output a new Thought and Action block."
                )
            })
            response_text = _get_llm_response(messages).strip()
            if not response_text or not response_text.strip():
                # Empty response – stop retrying
                break

            # Check for new tool definition
            if _attempt_define_tool(response_text):
                return "New tool defined. It will be available for future interactions."

            new_tool_calls = _collect_tool_calls(response_text)
            if not new_tool_calls:
                # LLM chose to answer without tools
                return response_text

            # Execute new round of tools
            new_results: list[str] = []
            for tc2 in new_tool_calls:
                action = tc2.get("action", "")
                args = tc2.get("args", "")
                if not isinstance(args, str):
                    args = str(args)
                result2 = _run_tool(action, args)
                print(f"[Tool] {action}({args!r}) → {result2[:200]}...")
                new_results.append(f"- `{action}({args!r})` returned:\n{result2[:2000]}")
            tool_results_text = "\n".join(new_results)

            if not any(_is_tool_error(r) for r in new_results):
                # Success – now produce a natural answer
                all_ok = True
                break
            # Continue loop to retry again

    if all_ok:
        # Build a summary prompt for the final answer
        summary_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Kyrozen, an intelligent AI assistant. "
                    "You have just obtained the following information by running tools. "
                    "Now produce a clear, natural answer to the user's original request. "
                    "Do **not** output any Action, JSON, or tool calls – only plain text."
                )
            },
            {
                "role": "system",
                "content": f"You are working inside {os.getcwd()}."
            },
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": response_text},
            {
                "role": "user",
                "content": (
                    f"The tools returned:\n{tool_results_text}\n\n"
                    "Please respond directly with the analysis the user asked for."
                )
            }
        ]
        final_response = _get_llm_response(summary_messages).strip()
        if not final_response or len(final_response) < 10:
            final_response = f"I executed your request. Here is the information I gathered:\n\n{tool_results_text}"
        return final_response
    else:
        # All retries exhausted – produce a helpful failure message
        return (
            f"After {MAX_RETRIES} attempts the tool(s) still failed. "
            f"Last output:\n{tool_results_text}\n\n"
            "Suggestions:\n"
            "- Make sure the path is enclosed in double quotes (e.g. `cd \"a( b)\"`).\n"
            "- Check that the directory exists.\n"
            "- Run a simpler command first (`pwd`, `ls`) to verify location.\n"
            "- Use `/quit` to exit, fix the issue, then restart."
        )



def _split_reply(text: str) -> tuple[str, str]:
    """Return (thinking_part, answer_part) for display."""
    text = text.strip()
    # Attempt to locate a Thought: section
    thought_match = re.search(r"^Thought:\s*(.*?)(?=\n(?:Action:|(?:\n|$)))", text, re.DOTALL | re.MULTILINE)
    if thought_match:
        thinking = thought_match.group(1).strip()
        # Remove the thought section from the whole text to get the answer
        answer = re.sub(r"^Thought:\s*.*?(?=\n(?:Action:|(?:\n|$))|\n?$)", "", text, count=1, flags=re.DOTALL | re.MULTILINE).strip()
        # Also strip any leftover Action blocks (they should have been executed, but LLM may keep them)
        answer = re.sub(r"Action:\s*```(?:json)?[\s\S]*?```", "", answer).strip()
        answer = re.sub(r"\{[\s\S]*?\}", "", answer).strip()  # remove stray JSON objects
        answer = answer.lstrip('"').lstrip("'").strip()
        # If after stripping there's nothing, fallback to original
        if not answer:
            answer = text
        return thinking, answer
    else:
        # No explicit thought – treat whole text as answer
        return "", text


def main() -> None:
    global deepseek_client, DEEPSEEK_MODEL
    banner_text = "OpenKyrozen – Self‑learning AI Agent"
    console.print(Panel.fit(banner_text, title="OPEN KYROZEN", subtitle="self‑learning AI agent", border_style="cyan"))
    console.print(f"Kyrozen (DeepSeek + Tools). Model: {MODEL_NAME}", style="cyan")
    console.print("Commands: /quit or /exit to exit, /save to long‑term memory, /learn to reload project files, /api_key to change API key.\n", style="yellow")

    _prompt_and_init_deepseek()
    if deepseek_client is None:
        console.print("Cannot start without an API key.", style="red")
        sys.exit(1)
    _load_project_files_into_memory()

    while True:
        try:
            user_input = console.input("[bold cyan]You: [/bold cyan]").strip()
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
        if user_input.lower() == "/api_key":
            new_key = console.input("[bold yellow]Enter new DeepSeek API key: [/bold yellow]").strip()
            if new_key:
                _save_config_key(new_key)
                os.environ["DEEPSEEK_API_KEY"] = new_key
                base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
                DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
                deepseek_client = OpenAI(api_key=new_key, base_url=base_url)
                console.print("[green]API key updated and saved for future sessions.[/green]")
            else:
                console.print("[red]No key provided – key unchanged.[/red]")
            continue

        reply = _chat_turn(user_input)

        if len(reply.strip()) < 5:
            console.print("[red][Error] Received empty response from LLM[/red]")
            continue

        short_term_memory.append({"role": "user", "content": user_input})
        short_term_memory.append({"role": "assistant", "content": reply})
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        # Separate thinking from the final answer
        thinking, answer = _split_reply(reply)

        if thinking:
            console.print(Panel(thinking, title="Thinking", border_style="dim white"))
        if answer:
            console.print(Panel(Markdown(answer), title="Agent", border_style="green"))
        else:
            console.print(Panel(Markdown(answer or "(no content)"), title="Agent", border_style="green"))
        print()  # spacing


if __name__ == "__main__":
    main()
