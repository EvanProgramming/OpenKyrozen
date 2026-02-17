"""
AI Agent: optimized for low-memory (7B) models. Ollama + tools + short prompt.
"""

import json
import re
import sys

from ollama import chat as ollama_chat
from ollama import Client as OllamaClient

from memory import MemoryBank
from tools import AVAILABLE_TOOLS


# ---- Constants (tuned for 7B) ----
MODEL_NAME = "qwen2.5-coder:7b"
SHORT_TERM_CAP = 8  # fewer turns to save context window
MAX_TOOL_RETRIES = 3


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
    """Minimalist system prompt for 7B models (no external files)."""
    return f"""You are a helpful AI Assistant with access to tools.

## Tools Available:
{tools_list}

## Instructions:
To use a tool, you MUST use the following format:

Thought: Explain your reasoning here.
Action:
```json
{{
  "action": "tool_name",
  "args": "arguments"
}}
```

If no tool is needed, just reply normally.
ALWAYS start with "Thought:" if you are solving a task.
"""


# ---- Shared state ----
ollama = OllamaClient()
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
    recalled = memory_bank.recall(user_input, n_results=2)
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


def _get_llm_response(messages: list[dict[str, str]]) -> str:
    """Call Ollama; return assistant content. Debug-prints messages."""
    print("[DEBUG] Messages sent to ollama.chat:")
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        preview = content[:80] + "..." if len(content) > 80 else content
        print(f"  [{i}] role={role!r} content={preview!r}")
    print()
    try:
        response = ollama_chat(model=MODEL_NAME, messages=messages)
        return (response.message and getattr(response.message, "content", None)) or ""
    except Exception as e:
        return f"[LLM Error] {e}"


def _chat_turn(user_input: str) -> str:
    """One user turn: build context, get LLM reply, handle tool call and retry if empty."""
    messages = _build_messages(user_input)
    response_text = _get_llm_response(messages).strip()

    # Robust retry for empty response
    print(f"[DEBUG RAW]: {repr(response_text)}")
    if not response_text or not response_text.strip():
        print("[Warning] Empty response. Retrying with explicit instruction...")
        messages.append({
            "role": "user",
            "content": "System: You returned nothing. Please output your Thought and JSON Action now.",
        })
        response_text = _get_llm_response(messages).strip()
        print(f"[DEBUG RAW]: {repr(response_text)}")

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


def main() -> None:
    print("AI Agent (Ollama + Tools). Model:", MODEL_NAME)
    print("Commands: /quit exit, /save save to long-term memory.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            sys.exit(0)

        if not user_input:
            continue
        if user_input.lower() == "/quit":
            print("Goodbye.")
            break
        if user_input.lower() == "/save":
            memory_bank.add_log(
                "Conversation: " + "; ".join(
                    m.get("content", "")[:200] for m in short_term_memory[-6:] if m.get("content")
                )
            )
            print("Saved to long-term memory.")
            continue

        reply = _chat_turn(user_input)

        print(f"[DEBUG RAW]: {repr(reply)}")

        if len(reply.strip()) < 5:
            print("[Error] Received empty response from LLM")
            continue

        short_term_memory.append({"role": "user", "content": user_input})
        short_term_memory.append({"role": "assistant", "content": reply})
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        print("Agent:", reply, "\n")


if __name__ == "__main__":
    main()
