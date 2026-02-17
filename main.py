"""
AI Agent: core loop with Ollama, long/short-term memory, and tool use.
Fixes: Amnesia (context from RAG + last 10 turns), Blank responses (re-prompt after tool).
"""

import json
import re
import sys

from ollama import chat as ollama_chat
from ollama import Client as OllamaClient

from memory import MemoryBank
from tools import AVAILABLE_TOOLS


# ---- Constants ----
MODEL = "gpt-oss:20b"
SHORT_TERM_CAP = 10  # last N conversation turns
MAX_TOOL_RETRIES = 3

SYSTEM_PROMPT = """
        You are a powerful, autonomous AI Agent running LOCALLY on the user's computer.
        You have FULL PERMISSION to create, read, and modify files on this system.
        
        [AVAILABLE TOOLS]:
        {tools_list}
        
        [Current Context]:
        {memory_context}
        
        [INSTRUCTIONS]:
        1. When the user asks to create a file, DO IT using the `write_file` tool.
        2. Do NOT say "I cannot do that". You CAN.
        3. Do NOT provide shell commands for the user to run. RUN THEM YOURSELF using the tools.
        4. For file paths:
           - Use `~/Desktop/filename.txt` for the user's desktop.
           - Use `.` for the current directory.
        
        [RESPONSE FORMAT]:
        You must first output your thought process, then the tool usage JSON.
        
        Example:
        Thought: The user wants to create a file on the desktop. I will use the write_file tool.
        Action: ```json
        {{
          "action": "write_file",
          "args": "~/Desktop/test.txt|Content here"
        }}
        ```
        """


def _build_tools_list() -> str:
    """Build tools list for system prompt: each tool name + its docstring (description)."""
    lines = []
    for name, fn in AVAILABLE_TOOLS.items():
        doc = getattr(fn, "__doc__", None) or ""
        desc = doc.strip().replace("\n", " ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


TOOLS_LIST = _build_tools_list()


# ---- Shared state (outside main loop to avoid amnesia) ----
ollama = OllamaClient()
memory_bank = MemoryBank()
short_term_memory: list[dict[str, str]] = []


def _build_messages(user_input: str) -> list[dict[str, str]]:
    """Build full message list: system + RAG context + last 10 turns + current user."""
    messages: list[dict[str, str]] = []

    # 1. Long-term memory (top 2 relevant logs) for context placeholder
    recalled = memory_bank.recall(user_input, n_results=2)
    memory_context = ("Relevant past context:\n" + "\n---\n".join(recalled)) if recalled else "(none)"

    # 2. System prompt (with tools_list and memory_context filled in)
    system_content = SYSTEM_PROMPT.format(tools_list=TOOLS_LIST, memory_context=memory_context)
    messages.append({"role": "system", "content": system_content})

    # 3. Last N turns (keep pairs: user + assistant)
    for msg in short_term_memory[-SHORT_TERM_CAP * 2 :]:
        messages.append(msg)

    # 4. Current user input
    messages.append({"role": "user", "content": user_input})

    return messages


def parse_json_from_response(text: str) -> dict | None:
    """
    Extract tool-call JSON only from a ```json ... ``` code block in the response.
    Ignores "Thought:" and any other text; if there is no valid json block with
    action/args, returns None (so Thought-only responses are normal conversation).
    """
    text = (text or "").strip()
    # Only look inside a block explicitly labeled as json (CoT format)
    code_match = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if not code_match:
        return None
    raw = code_match.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "action" in data and data.get("action") in AVAILABLE_TOOLS:
            return data
    except json.JSONDecodeError:
        pass
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
    """Call Ollama and return assistant content. Debug-prints messages before calling."""
    print("[DEBUG] Messages sent to ollama.chat:")
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        preview = content[:80] + "..." if len(content) > 80 else content
        print(f"  [{i}] role={role!r} content={preview!r}")
    print()
    try:
        response = ollama_chat(model=MODEL, messages=messages)
        return (response.message and getattr(response.message, "content", None)) or ""
    except Exception as e:
        return f"[LLM Error] {e}"


def _chat_turn(user_input: str) -> str:
    """
    One user turn. Flow: build context → LLM reply. If tool call:
    Step A: Parse JSON. Step B: Execute tool. Step C: Tool feedback (user role).
    Step D: Call LLM with (History + JSON_Command + Tool_Feedback). Step E: Return final response.
    """
    messages = _build_messages(user_input)
    response_text = _get_llm_response(messages).strip()

    for attempt in range(MAX_TOOL_RETRIES + 1):
        # Step A: Parse JSON (only from ```json ... ``` block; ignore Thought:)
        tool_call = parse_json_from_response(response_text)
        if not tool_call:
            return response_text

        action = tool_call.get("action", "")
        args = tool_call.get("args", "") if isinstance(tool_call.get("args"), str) else str(tool_call.get("args", ""))

        # Step B: Execute tool
        result = _run_tool(action, args)

        if result.strip().lower().startswith("error") and attempt < MAX_TOOL_RETRIES:
            retry_messages = _build_messages(user_input)
            retry_messages.append({
                "role": "user",
                "content": f"System Notification: Tool failed. Result: {result}. Try again with a different action or args (reply with JSON only).",
            })
            response_text = _get_llm_response(retry_messages).strip()
            continue

        # Step C: Tool feedback message (user role only — no system)
        tool_feedback = (
            f"System Notification: Tool executed. Result: {result}. "
            "Please summarize what you did to the user."
        )

        # Step D: History + JSON command + Tool feedback → LLM
        follow_up = _build_messages(user_input)
        follow_up.append({"role": "assistant", "content": response_text})
        follow_up.append({"role": "user", "content": tool_feedback})
        response_text = _get_llm_response(follow_up).strip()
        if not response_text:
            response_text = result  # fallback to raw result if LLM stays blank
        break

    return response_text


def main() -> None:
    print("AI Agent (Ollama + Memory + Tools). Model:", MODEL)
    print("Commands: /quit to exit, /save to log current state to long-term memory.\n")

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
                "Conversation summary: " + "; ".join(
                    m.get("content", "")[:200] for m in short_term_memory[-6:] if m.get("content")
                )
            )
            print("Saved recent context to long-term memory.")
            continue

        reply = _chat_turn(user_input)

        # Debug: raw LLM output (repr shows hidden newlines/spaces)
        print(f"   [Raw LLM Output]: {repr(reply)}")

        # Relaxed empty check: Thought-only or short noise is not "empty"
        if len(reply.strip()) < 5:
            print("[Error] Received empty response from LLM")
            continue

        # Persist this turn in short-term memory (last 10 turns for context)
        short_term_memory.append({"role": "user", "content": user_input})
        short_term_memory.append({"role": "assistant", "content": reply})

        # Log to long-term for future RAG
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        # Step E: Print final human-readable response
        print("Agent:", reply, "\n")


if __name__ == "__main__":
    main()
