"""
OpenKyrozen Web Server — FastAPI + Chat UI + REST API
Run: kyrozen-web  or  uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import sys
import json
import time
import threading
import uuid
import hmac
import ipaddress
import copy
import re
import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Add parent to path so we can import main
sys.path.insert(0, str(Path(__file__).parent))

# Minimal env setup for headless mode
os.environ.setdefault("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
# A Web/MCP process is a separate execution surface from the local CLI. The
# main module therefore keeps dynamic Python tools off unless explicitly
# enabled for this server process, while ordinary workspace tools remain
# available through capability profiles below.
os.environ.setdefault("KYROZEN_EXECUTION_SURFACE", "web")

import main as _agent
from providers import get_cost_summary, reset_cost_tracker
from memory import MemoryBank
from task_engine import TaskManager, TaskWorker
from scheduler import JobScheduler
from tools import allowed_tool_names, resolve_capabilities, tool_capability
from capability_tokens import issue_capability_token

try:
    from fastapi import FastAPI, Request, HTTPException, Depends
    from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    sys.exit("Install FastAPI: pip install fastapi uvicorn")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenKyrozen API",
    description="Self-learning AI Agent — REST API + Web Chat",
    version="2.0.0",
)

# ---------------------------------------------------------------------------
# API access control
# ---------------------------------------------------------------------------

_SERVER_TOKEN = os.environ.get("KYROZEN_SERVER_TOKEN", "").strip()
# A server process is deliberately single-user.  The bearer token authenticates
# access to this deployment; it is not a multi-user identity provider.  Keep
# this actor stable across loopback/token access and token rotation so existing
# private state remains owned by the same deployment user.
_CONFIGURED_SERVER_ACTOR = os.environ.get("KYROZEN_SERVER_ACTOR", "local").strip()
_SERVER_ACTOR_ID = (_CONFIGURED_SERVER_ACTOR
                   if re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", _CONFIGURED_SERVER_ACTOR)
                   else "local")
_LOCAL_CLIENT_NAMES = {"localhost", "127.0.0.1", "::1", "testclient"}


def _is_loopback_client(request: Request) -> bool:
    """Return True only for a direct loopback/test client connection."""
    host = request.client.host if request.client else ""
    if host in _LOCAL_CLIENT_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_api_access(request: Request) -> None:
    """Protect API/MCP routes with a bearer token or direct loopback access."""
    if _SERVER_TOKEN:
        supplied = request.headers.get("authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = request.headers.get("x-kyrozen-token", "").strip()
        if hmac.compare_digest(supplied, _SERVER_TOKEN):
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if _is_loopback_client(request):
        return
    raise HTTPException(
        status_code=503,
        detail="Set KYROZEN_SERVER_TOKEN before exposing the server beyond localhost",
    )


def _sanitize_api_message(message: str) -> str:
    """Apply the same prompt-injection filter to headless API requests."""
    sanitized, flagged = _agent._sanitize_input(message)
    if flagged:
        _audit("PROMPT_INJECTION_FILTERED", "API message contained a known pattern")
    return sanitized

# Audit log
def _audit_log_path() -> Path:
    explicit = os.environ.get("KYROZEN_AUDIT_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return _agent._state_root() / "kyrozen_audit.log"

def _audit(event: str, detail: str = "", user: str = "anonymous") -> None:
    """Append an audit entry to the log file."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        audit_path = _audit_log_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write(f"[{ts}] [{user}] {event} | {detail}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_sessions: dict[str, dict[str, Any]] = {}  # session_id -> {messages, user_id, created}
_chat_lock = threading.RLock()
_MAX_SESSION_MESSAGES = 32
_MAX_MESSAGE_CHARS = 12_000
_MAX_MEMORY_QUERY_CHARS = 500
_MAX_MEMORY_RESULTS = 50
# The web surface must use the authenticated deployment actor for every
# durable subsystem.  Request JSON can describe a public/group speaker, but
# it can never change ownership of private state.
_agent.memory_bank.user_id = _SERVER_ACTOR_ID
_agent.tasks.user_id = _SERVER_ACTOR_ID
_scheduler = JobScheduler(_agent.memory_bank.store, workspace_id=_agent.memory_bank.workspace_id,
                          user_id=_SERVER_ACTOR_ID)


def _execute_durable_task(task: dict[str, Any]) -> dict[str, Any]:
    """Execute only an explicitly stored, Web-profile action."""
    checkpoint = task.get("checkpoint", {})
    action = str(checkpoint.get("action", "")).strip()
    args = checkpoint.get("args", "")
    if not action:
        return {"success": False,
                "result": "No executable action stored; create the task with action and string args."}
    action = _agent.TOOL_ALIASES.get(action, action)
    if not isinstance(args, str):
        return {"success": False, "action": action,
                "result": "Task args must be a plain string."}
    if action not in _allowed_server_tools("web"):
        return {"success": False, "action": action,
                "result": f"Error: task action '{action}' is not allowed by the Web capability profile."}
    _agent._execution_capability_token = issue_capability_token(
        "surface:web:durable-task", _server_capabilities("web"), ttl_seconds=300,
    )
    result = str(_agent._run_tool(action, args))[:2000]
    return {
        "success": not result.lower().startswith("error:"), "action": action, "args": args,
        "result": result,
        "acceptance": str(checkpoint.get("acceptance") or "durable task action completed"),
    }


def _task_manager(session_id: str | None) -> TaskManager:
    """Build a task manager bound to the authenticated deployment scope."""
    return TaskManager(
        _agent.memory_bank.store,
        workspace_id=_agent.memory_bank.workspace_id,
        session_id=session_id,
        user_id=_SERVER_ACTOR_ID,
    )


def _task_worker(session_id: str | None) -> TaskWorker:
    """Build a bounded worker that can only claim one exact session scope."""
    return TaskWorker(_task_manager(session_id), _execute_durable_task, max_tasks=1)


def _task_scopes(statuses: set[str]) -> set[str | None]:
    rows = _agent.memory_bank.store.list_tasks(
        workspace_id=_agent.memory_bank.workspace_id,
        statuses=statuses,
        limit=10000,
        user_id=_SERVER_ACTOR_ID,
    )
    return {row.get("session_id") for row in rows}


def _recover_task_scopes() -> list[dict[str, Any]]:
    """Recover every unfinished task without collapsing session boundaries."""
    scopes = _task_scopes({"pending", "running", "failed", "blocked"})
    recovered: list[dict[str, Any]] = []
    for session_id in scopes:
        recovered.extend(_task_manager(session_id).recover())
    return recovered


def _run_task_worker_cycle() -> None:
    """Run at most one safe action per pending session in this scheduler tick."""
    for session_id in sorted(_task_scopes({"pending"}), key=lambda item: item or ""):
        _task_worker(session_id).run_once()


def _run_scheduled_job(job: dict[str, Any]) -> None:
    payload = job.get("payload", {})
    if payload.get("type") == "learning_cycle":
        _agent.dispatch_learning_cycle(surface="web", trigger="scheduled", max_features=4)
        return
    if payload.get("type") == "task_worker":
        _run_task_worker_cycle()
        return
    if payload.get("type") == "chat":
        session_id = _normalise_session_id(payload.get("session_id"))
        session = _get_or_create_session(session_id, _SERVER_ACTOR_ID)
        _run_session_chat(session, str(payload.get("message", "")))
        return
    raise ValueError(f"Unknown scheduled job type: {payload.get('type', 'default')}")


_scheduler.register_callback("learning_cycle", _run_scheduled_job)
_scheduler.register_callback("chat", _run_scheduled_job)
_scheduler.register_callback("task_worker", _run_scheduled_job)


def _normalise_session_id(raw_session_id: Any) -> str:
    """Validate a client session key without allowing unbounded map growth."""
    if raw_session_id is None or raw_session_id == "":
        return f"sess_{uuid.uuid4().hex}"
    session_id = str(raw_session_id).strip()
    if len(session_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", session_id):
        raise HTTPException(400, "Invalid session_id")
    return session_id


def _normalise_profile(raw_profile: Any) -> str:
    profile = str(raw_profile or "auto").strip().lower()
    if profile not in {"auto", "coder", "researcher"}:
        raise HTTPException(400, "profile must be auto, coder, or researcher")
    return profile


def _normalise_memory_actor(value: Any, field: str, default: str) -> str:
    actor = str(value or default).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", actor):
        raise HTTPException(400, f"Invalid {field}")
    return actor


def _normalise_memory_context(speaker: Any, audience: Any, channel: Any) -> tuple[str | None, str | None, str | None]:
    speaker = _normalise_memory_actor(speaker, "speaker", "local") if speaker else None
    audience = _normalise_memory_actor(audience, "audience", speaker or "local") if audience or speaker else None
    channel = _normalise_memory_actor(channel, "channel", "chat") if channel else None
    return speaker, audience, channel


def _set_memory_context(session: dict[str, Any], body: dict[str, Any]) -> None:
    default = _SERVER_ACTOR_ID
    session["user_id"] = default
    speaker = _normalise_memory_actor(body.get("speaker"), "speaker", session.get("speaker", default))
    audience = _normalise_memory_actor(body.get("audience"), "audience", session.get("audience", speaker))
    channel = _normalise_memory_actor(body.get("channel"), "channel", session.get("channel", "chat"))
    session.update({"speaker": speaker, "audience": audience, "channel": channel,
                    "authorized_speakers": [default]})


def _actor_for_request(request: Request) -> str:
    """Return the stable single-user deployment actor.

    Authentication is enforced by ``require_api_access``.  This function only
    maps an already-authorized request to the one owner supported by this
    server process; no header or JSON field can select another private user.
    """
    return _SERVER_ACTOR_ID


def _get_or_create_session(session_id: str, user_id: str = "anonymous") -> dict:
    user_id = _SERVER_ACTOR_ID
    with _chat_lock:
        if session_id not in _sessions:
            persisted = _agent.memory_bank.store.list_events(
                event_type="session.message", limit=_MAX_SESSION_MESSAGES,
                workspace_id=_agent.memory_bank.workspace_id, session_id=session_id,
                user_id=user_id,
            )
            messages = []
            for event in reversed(persisted):
                payload = event.get("payload", {})
                if payload.get("role") in {"user", "assistant"} and payload.get("content"):
                    messages.append({"role": payload["role"], "content": payload["content"]})
            _sessions[session_id] = {
                "messages": messages[-_MAX_SESSION_MESSAGES:],
                "user_id": user_id,
                "session_id": session_id,
                "created": time.time(),
            }
            # Keep only last 100 sessions
            if len(_sessions) > 100:
                oldest = min(_sessions, key=lambda k: _sessions[k]["created"])
                del _sessions[oldest]
        return _sessions[session_id]


def _run_session_chat(session: dict[str, Any], message: str) -> str:
    """Run the legacy global agent with an isolated per-session context.

    The core agent currently stores short-term messages and task state in module
    globals. Serialising the swap prevents concurrent requests from seeing one
    another while preserving independent histories in the web layer.
    """
    with _chat_lock:
        previous_messages = _agent.short_term_memory
        previous_memory_session = _agent.memory_bank.session_id
        previous_tasks_ref = _agent.tasks.tasks
        previous_tasks = copy.deepcopy(_agent.tasks.tasks)
        previous_tools = dict(_agent.AVAILABLE_TOOLS)
        previous_tools_list = _agent.TOOLS_LIST
        previous_learning_run = _agent._last_learning_run
        previous_learning_notices = _agent._learning_notices
        try:
            previous_recall = _agent.memory_bank.store.list_events(
                "memory.recalled", limit=1, workspace_id=_agent.memory_bank.workspace_id,
                session_id=session.get("session_id"), user_id=_SERVER_ACTOR_ID,
            )
            previous_recall_id = previous_recall[0]["id"] if previous_recall else None
            allowed = _allowed_server_tools("web")
            _agent.AVAILABLE_TOOLS.clear()
            _agent.AVAILABLE_TOOLS.update({
                name: fn for name, fn in previous_tools.items() if name in allowed
            })
            _agent.TOOLS_LIST = _agent._build_tools_list()
            _agent.short_term_memory = list(session["messages"])
            _agent._last_learning_run = session.get("last_learning_run")
            _agent._learning_notices = []
            _agent.memory_bank.session_id = session.get("session_id") or session.setdefault("session_id", _normalise_session_id(session.get("id")))
            profile = session.get("profile", "auto")
            memory_context = {key: session.get(key) for key in (
                "speaker", "audience", "channel", "authorized_speakers")}
            reply = (_agent._chat_turn(message, clear_tasks=True, profile=profile, memory_context=memory_context)
                     if profile != "auto" else _agent._chat_turn(message, clear_tasks=True,
                                                                  memory_context=memory_context))
            session["last_learning_run"] = _agent._last_learning_run
            _agent.short_term_memory.extend([
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ])
            session["messages"] = _agent.short_term_memory[-_MAX_SESSION_MESSAGES:]
            session["updated"] = time.time()
            recalls = _agent.memory_bank.store.list_events(
                "memory.recalled", limit=1, workspace_id=_agent.memory_bank.workspace_id,
                session_id=_agent.memory_bank.session_id, user_id=_SERVER_ACTOR_ID,
            )
            session["last_memory_receipt"] = (recalls[0]["payload"]
                                              if recalls and recalls[0]["id"] != previous_recall_id else None)
            for role, content in (("user", message), ("assistant", reply)):
                _agent.memory_bank.store.append_event(
                    "session.message", {"role": role, "content": content},
                    user_id=session.get("user_id", "anonymous"),
                    workspace_id=_agent.memory_bank.workspace_id,
                    session_id=_agent.memory_bank.session_id,
                )
            return reply
        finally:
            new_dynamic_tools = {
                name: fn for name, fn in _agent.AVAILABLE_TOOLS.items()
                if name not in previous_tools and tool_capability(name) == "dynamic"
            }
            _agent.short_term_memory = previous_messages
            _agent.memory_bank.session_id = previous_memory_session
            previous_tasks_ref.clear()
            previous_tasks_ref.extend(previous_tasks)
            _agent.tasks.tasks = previous_tasks_ref
            _agent.AVAILABLE_TOOLS.clear()
            _agent.AVAILABLE_TOOLS.update(previous_tools)
            if new_dynamic_tools and _agent.ALLOW_DYNAMIC_TOOLS:
                _agent.AVAILABLE_TOOLS.update(new_dynamic_tools)
                _agent.TOOLS_LIST = _agent._build_tools_list()
            else:
                _agent.TOOLS_LIST = previous_tools_list
            _agent._last_learning_run = previous_learning_run
            _agent._learning_notices = previous_learning_notices


def _validate_message(message: str) -> str:
    if len(message) > _MAX_MESSAGE_CHARS:
        raise HTTPException(413, f"Message exceeds {_MAX_MESSAGE_CHARS} characters")
    return message


def _server_capabilities(surface: str) -> frozenset[str]:
    """Return the configured capability set for Web or MCP requests."""
    env_name = "KYROZEN_MCP_CAPABILITIES" if surface == "mcp" else "KYROZEN_WEB_CAPABILITIES"
    # workspace is intentionally rich: it includes file writes, shell, network
    # and ordinary Git operations. `full` additionally enables reset/dynamic.
    configured = os.environ.get(env_name, "workspace")
    if os.environ.get("KYROZEN_MCP_ALLOW_DANGEROUS", "").lower() in {"1", "true", "yes"}:
        configured = "full"
    return resolve_capabilities(configured, default="workspace")


def _allowed_server_tools(surface: str) -> set[str]:
    capabilities = ",".join(sorted(_server_capabilities(surface)))
    return allowed_tool_names(_agent.AVAILABLE_TOOLS, capabilities)

# ---------------------------------------------------------------------------
# HTML Chat UI
# ---------------------------------------------------------------------------

CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenKyrozen Chat</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;height:100vh;display:flex;flex-direction:column}
header{background:#161b22;padding:12px 20px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:12px}
header h1{font-size:18px;color:#00f0ff}
header span{font-size:12px;color:#8b949e}
#chat{flex:1;overflow-y:auto;padding:20px}
.msg{margin-bottom:16px;max-width:85%}
.msg.user{margin-left:auto}
.msg .role{font-size:11px;color:#8b949e;margin-bottom:4px}
.msg .content{background:#161b22;padding:12px 16px;border-radius:8px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.msg.user .content{background:#1f6feb22;border:1px solid #1f6feb44}
.msg.assistant .content{background:#161b22;border:1px solid #30363d}
.msg.thinking .content{background:#1a1a2e;border:1px solid #00f0ff22;color:#8b949e;font-style:italic}
#input-area{background:#161b22;padding:16px 20px;border-top:1px solid #30363d;display:flex;gap:12px}
#user-input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px;color:#c9d1d9;font-size:14px;resize:none;min-height:44px;max-height:120px}
#user-input:focus{outline:none;border-color:#00f0ff}
button{background:#00f0ff;color:#0d1117;border:none;border-radius:8px;padding:12px 20px;font-weight:600;cursor:pointer;transition:opacity .2s}
button:hover{opacity:.8}
button:disabled{opacity:.4;cursor:default}
#status{font-size:11px;color:#8b949e;text-align:center;padding:4px}
.cost{font-size:11px;color:#00ff88}
.error{color:#ff4466}
</style>
</head>
<body>
<header>
  <h1>OpenKyrozen</h1>
  <span>Self-learning AI Agent</span>
  <span class="cost" id="cost-display"></span>
</header>
<div id="chat"></div>
<div id="input-area">
  <textarea id="user-input" placeholder="Type your message..." rows="1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage()}"></textarea>
  <button onclick="sendMessage()" id="send-btn">Send</button>
</div>
<div id="status">Ready</div>
<script>
const sessionId = 'sess_' + Math.random().toString(36).slice(2,10);
let isStreaming = false;

function addMessage(role, content, isThinking) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg ' + role + (isThinking ? ' thinking' : '');
  div.innerHTML = `<div class="role">${role === 'assistant' ? 'Kyrozen' : role === 'user' ? 'You' : role}</div><div class="content">${escapeHtml(content)}</div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function escapeHtml(text) {
  return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function sendMessage() {
  const input = document.getElementById('user-input');
  const btn = document.getElementById('send-btn');
  const msg = input.value.trim();
  if (!msg || isStreaming) return;
  input.value = '';
  isStreaming = true;
  btn.disabled = true;
  document.getElementById('status').textContent = 'Thinking...';
  addMessage('user', msg, false);

  const assistantDiv = addMessage('assistant', '', false);
  const contentDiv = assistantDiv.querySelector('.content');

  try {
    const resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, session_id: sessionId})
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let full = '';
    let sseBuffer = '';
    const streamState = {done: false, error: false, parseError: false};

    function handleSseEvent(eventText) {
      const data = eventText.split(/\r?\n/)
        .filter(line => line.startsWith('data:'))
        .map(line => line.startsWith('data: ') ? line.slice(6) : line.slice(5))
        .join('\n');
      if (!data) return;
      if (data === '[DONE]') {
        streamState.done = true;
        return;
      }
      try {
        const parsed = JSON.parse(data);
        if (Object.prototype.hasOwnProperty.call(parsed, 'chunk')) {
          full += String(parsed.chunk);
          contentDiv.textContent = full;
        }
        if (Object.prototype.hasOwnProperty.call(parsed, 'cost')) {
          document.getElementById('cost-display').textContent = String(parsed.cost);
        }
        if (Object.prototype.hasOwnProperty.call(parsed, 'error')) {
          streamState.error = true;
          contentDiv.textContent = 'Error: ' + String(parsed.error);
          contentDiv.classList.add('error');
        }
      } catch (e) {
        streamState.parseError = true;
        contentDiv.textContent = 'Stream parse error: ' + e.message;
        contentDiv.classList.add('error');
      }
    }

    function consumeSseText(text) {
      sseBuffer += text;
      while (true) {
        const boundary = sseBuffer.match(/\r?\n\r?\n/);
        if (!boundary) break;
        const eventText = sseBuffer.slice(0, boundary.index);
        sseBuffer = sseBuffer.slice(boundary.index + boundary[0].length);
        handleSseEvent(eventText);
      }
    }

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      consumeSseText(decoder.decode(value, {stream: true}));
      document.getElementById('chat').scrollTop = document.getElementById('chat').scrollHeight;
    }
    consumeSseText(decoder.decode());
    if (sseBuffer.trim()) handleSseEvent(sseBuffer);
    if (streamState.parseError) {
      document.getElementById('status').textContent = 'Error';
    } else if (streamState.done) {
      document.getElementById('status').textContent = 'Ready';
    } else if (streamState.error) {
      document.getElementById('status').textContent = 'Error';
    } else {
      document.getElementById('status').textContent = 'Incomplete stream';
    }
  } catch(e) {
    contentDiv.textContent = 'Connection error: ' + e.message;
    contentDiv.classList.add('error');
    document.getElementById('status').textContent = 'Error';
  }
  isStreaming = false;
  btn.disabled = false;
  input.focus();
}

// Load cost on start
fetch('/api/cost').then(r=>r.json()).then(d=>{
  if (d.summary) document.getElementById('cost-display').textContent = 'Cost: ' + d.summary;
});
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def chat_page():
    return CHAT_HTML


@app.post("/api/chat", dependencies=[Depends(require_api_access)])
async def api_chat(request: Request):
    """Non-streaming chat endpoint."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    msg = _validate_message(_sanitize_api_message(str(body.get("message", "")).strip()))
    if not msg:
        raise HTTPException(400, "Empty message")
    session_id = _normalise_session_id(body.get("session_id"))
    session = _get_or_create_session(session_id, _actor_for_request(request))
    session["profile"] = _normalise_profile(body.get("profile", session.get("profile", "auto")))
    _set_memory_context(session, body)
    _audit("CHAT", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    try:
        reply = _run_session_chat(session, msg)
    except Exception as e:
        _audit("ERROR", str(e), session["user_id"])
        raise HTTPException(500, str(e))

    _emit_chat_completed(session, reply, streamed=False)
    _audit("REPLY", f"len={len(reply)}", session["user_id"])
    return {"reply": reply, "session_id": session_id, "profile": session["profile"],
            "memory_receipt": session.get("last_memory_receipt"), "cost": get_cost_summary()}


@app.post("/api/chat/stream", dependencies=[Depends(require_api_access)])
async def api_chat_stream(request: Request):
    """SSE streaming chat endpoint."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    msg = _validate_message(_sanitize_api_message(str(body.get("message", "")).strip()))
    if not msg:
        raise HTTPException(400, "Empty message")
    session_id = _normalise_session_id(body.get("session_id"))
    session = _get_or_create_session(session_id, _actor_for_request(request))
    session["profile"] = _normalise_profile(body.get("profile", session.get("profile", "auto")))
    _set_memory_context(session, body)
    _audit("CHAT_STREAM", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    async def generate():
        try:
            # The legacy agent is synchronous and may perform network and disk
            # I/O. Keep it off the ASGI event loop so one chat cannot stall
            # health checks or unrelated requests.
            reply = await asyncio.to_thread(_run_session_chat, session, msg)
            # Send chunks (simulated streaming for non-streaming providers)
            chunk_size = 20
            for i in range(0, len(reply), chunk_size):
                chunk = reply[i:i+chunk_size]
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                await asyncio_sleep(0.01)
            yield f"data: {json.dumps({'cost': get_cost_summary()})}\n\n"
            if session.get("last_memory_receipt"):
                yield f"data: {json.dumps({'memory_receipt': session['last_memory_receipt']})}\n\n"
            yield "data: [DONE]\n\n"
            _emit_chat_completed(session, reply, streamed=True)
            _audit("REPLY_STREAM", f"len={len(reply)}", session["user_id"])
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/memory", dependencies=[Depends(require_api_access)])
async def api_memory(q: str = "", limit: int = 10):
    """Search stored memories."""
    q = q[:_MAX_MEMORY_QUERY_CHARS]
    limit = max(1, min(limit, _MAX_MEMORY_RESULTS))
    if q:
        results = _agent.memory_bank.recall(q, n_results=limit)
    else:
        results = _agent.memory_bank.get_recent(limit)
    return {"results": results, "total": _agent.memory_bank.count_logs()}


@app.get("/api/v2/memory", dependencies=[Depends(require_api_access)])
async def api_v2_memory(request: Request, q: str = "", limit: int = 10, session_id: str | None = None,
                        speaker: str | None = None, audience: str | None = None,
                        channel: str | None = None):
    """Structured memory endpoint with scope and provenance metadata."""
    limit = max(1, min(limit, _MAX_MEMORY_RESULTS))
    speaker, audience, channel = _normalise_memory_context(speaker, audience, channel)
    authorized_speakers = {_actor_for_request(request)}
    if q:
        memory = MemoryBank(_agent.memory_bank.db_path, user_id=_actor_for_request(request),
                            workspace_id=_agent.memory_bank.workspace_id,
                            session_id=_normalise_session_id(session_id) if session_id else None)
        results = memory.recall_records(q, n_results=limit, speaker=speaker, audience=audience, channel=channel,
                                        authorized_speakers=authorized_speakers)
    else:
        memory = MemoryBank(_agent.memory_bank.db_path, user_id=_actor_for_request(request),
                            workspace_id=_agent.memory_bank.workspace_id,
                            session_id=_normalise_session_id(session_id) if session_id else None)
        rows = memory.store.list_memories(status="active", limit=limit * 4,
                                          workspace_id=memory.workspace_id, session_id=memory.session_id,
                                          user_id=_actor_for_request(request))
        results = memory.filter_records(rows, speaker=speaker, audience=audience, channel=channel,
                                        authorized_speakers=authorized_speakers)[:limit]
    return {"results": results, "total": memory.count_logs(), "scope": {
        "workspace_id": memory.workspace_id, "session_id": memory.session_id,
    }}


@app.get("/api/v2/memory/claims", dependencies=[Depends(require_api_access)])
async def api_v2_memory_claims(request: Request, speaker: str | None = None, audience: str | None = None,
                               channel: str | None = None):
    speaker, audience, channel = _normalise_memory_context(speaker, audience, channel)
    rows = _agent.memory_bank.store.list_memories(status=None, limit=10000,
                                                  workspace_id=_agent.memory_bank.workspace_id,
                                                  user_id=_actor_for_request(request))
    claims = [row for row in rows if row.get("metadata", {}).get("claim")]
    return {"claims": _agent.memory_bank.filter_records(
        claims, speaker=speaker, audience=audience, channel=channel,
        authorized_speakers={_actor_for_request(request)},
    )}


@app.post("/api/v2/memory/claims", dependencies=[Depends(require_api_access)])
async def api_v2_create_memory_claim(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    actor = _actor_for_request(request)
    if body.get("claim_type") == "private_fact" and body.get("speaker") not in {None, actor}:
        raise HTTPException(403, "Private claims must belong to the authenticated speaker")
    if body.get("claim_type") == "private_fact":
        body["speaker"] = actor
    try:
        return _agent.learning_engine.remember_claim(
            key=body.get("key", ""), value=body.get("value", ""), kind=body.get("kind", "fact"),
            authority=body.get("authority", "owner"), scope=body.get("scope", "global"),
            scope_value=body.get("scope_value", ""), evidence_id=body.get("evidence_id"),
            claim_type=body.get("claim_type", "general"), speaker=body.get("speaker"),
            audiences=body.get("audiences") if isinstance(body.get("audiences"), list) else [],
            channel=body.get("channel"), visibility=body.get("visibility", "public"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v2/memory/claims/{claim_id}", dependencies=[Depends(require_api_access)])
async def api_v2_memory_claim(request: Request, claim_id: str, speaker: str | None = None,
                              audience: str | None = None, channel: str | None = None):
    speaker, audience, channel = _normalise_memory_context(speaker, audience, channel)
    actor = _actor_for_request(request)
    claim = _agent.learning_engine.explain_claim(
        claim_id, user_id=actor, workspace_id=_agent.memory_bank.workspace_id,
    )
    visible = _agent.memory_bank.filter_records(
        [claim] if claim else [], speaker=speaker, audience=audience, channel=channel,
        authorized_speakers={actor},
    )
    if not visible:
        raise HTTPException(404, "Memory claim not found")
    return visible[0]


@app.delete("/api/v2/memory/claims/{claim_id}", dependencies=[Depends(require_api_access)])
async def api_v2_memory_forget_claim(request: Request, claim_id: str):
    actor = _actor_for_request(request)
    if not _agent.learning_engine.forget_claim(
            claim_id, user_id=actor, workspace_id=_agent.memory_bank.workspace_id):
        raise HTTPException(404, "Memory claim not found")
    return {"status": "forgotten", "memory_id": claim_id}


@app.get("/api/v2/tasks", dependencies=[Depends(require_api_access)])
async def api_v2_tasks(session_id: str | None = None):
    session = _normalise_session_id(session_id) if session_id else None
    manager = _task_manager(session)
    return {"tasks": manager.store.list_tasks(workspace_id=manager.workspace_id, session_id=session,
                                                user_id=_SERVER_ACTOR_ID)}


@app.post("/api/v2/tasks", dependencies=[Depends(require_api_access)])
async def api_v2_create_task(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("description", "")).strip():
        raise HTTPException(400, "description is required")
    session = _normalise_session_id(body.get("session_id"))
    manager = _task_manager(session)
    checkpoint: dict[str, Any] = {}
    if body.get("action") is not None:
        action = _agent.TOOL_ALIASES.get(str(body.get("action")).strip(), str(body.get("action")).strip())
        if action not in _allowed_server_tools("web"):
            raise HTTPException(403, f"Task action '{action}' is not allowed by the Web capability profile")
        if not isinstance(body.get("args", ""), str):
            raise HTTPException(400, "args must be a plain string")
        checkpoint = {"action": action, "args": body.get("args", "")}
        if isinstance(body.get("acceptance"), list) and body["acceptance"]:
            checkpoint["acceptance"] = body["acceptance"][0]
    index = manager.add_task(
        str(body["description"]),
        dependencies=body.get("dependencies") if isinstance(body.get("dependencies"), list) else None,
        acceptance=body.get("acceptance") if isinstance(body.get("acceptance"), list) else None,
        priority=int(body.get("priority", 0)),
        checkpoint=checkpoint,
    )
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in _scheduler.list_jobs()):
        _scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    return {"task": manager.tasks[index], "queued": True}


@app.post("/api/v2/tasks/{task_id}/resume", dependencies=[Depends(require_api_access)])
async def api_v2_resume_task(task_id: str):
    candidates = _agent.memory_bank.store.list_tasks(
        workspace_id=_agent.memory_bank.workspace_id,
        statuses={"pending", "running", "failed", "blocked"},
        limit=10000,
        user_id=_SERVER_ACTOR_ID,
    )
    stored = next((item for item in candidates if item["id"] == str(task_id)), None)
    if stored is None:
        raise HTTPException(404, "Failed or blocked task not found in this scope")
    manager = _task_manager(stored.get("session_id"))
    manager.recover()
    task = manager.resume(task_id)
    if task is None:
        raise HTTPException(404, "Failed or blocked task not found in this scope")
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in _scheduler.list_jobs()):
        _scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    return {"task": task, "queued": True}


@app.get("/api/v2/learning", dependencies=[Depends(require_api_access)])
async def api_v2_learning(status: str | None = None, profile: str | None = None, limit: int = 50):
    if profile is not None:
        profile = _normalise_profile(profile)
        if profile == "auto":
            profile = None
    proposals = _agent.learning_engine.status(max(1, min(limit, 200)), profile=profile, status=status)
    return {"proposals": proposals}


@app.get("/api/v2/learning/metrics", dependencies=[Depends(require_api_access)])
async def api_v2_learning_metrics(profile: str | None = None):
    if profile is not None:
        profile = _normalise_profile(profile)
        if profile == "auto":
            profile = None
    return _agent.learning_engine.metrics(profile)


@app.get("/api/v2/learning/features", dependencies=[Depends(require_api_access)])
async def api_v2_learning_features():
    """Expose the authoritative registry and the latest durable run status."""
    return {"features": _agent.learning_feature_status()}


@app.get("/api/v2/learning/{proposal_id}/evidence", dependencies=[Depends(require_api_access)])
async def api_v2_learning_evidence(proposal_id: str):
    card = _agent.learning_engine.evidence_card(proposal_id)
    if card is None:
        raise HTTPException(404, "Learning proposal not found")
    return card


@app.post("/api/v2/learning/{proposal_id}/replay", dependencies=[Depends(require_api_access)])
async def api_v2_learning_replay(proposal_id: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    try:
        return _agent.learning_engine.record_shadow_replay(
            proposal_id, body.get("candidate", []), body.get("predecessor", []),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v2/learning/{proposal_id}/omission", dependencies=[Depends(require_api_access)])
async def api_v2_learning_omission(proposal_id: str, request: Request):
    body = await request.json()
    try:
        return _agent.learning_engine.record_omission_trial(
            proposal_id, body.get("with_item", []), body.get("without_item", []),
        )
    except (AttributeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v2/learning/{proposal_id}/retire", dependencies=[Depends(require_api_access)])
async def api_v2_learning_retire(proposal_id: str):
    if not _agent.learning_engine.retire_artifact(proposal_id):
        raise HTTPException(409, "Artifact lacks non-regressing omission evidence")
    return {"status": "retired", "proposal_id": proposal_id}


@app.post("/api/v2/learning/{proposal_id}/restore", dependencies=[Depends(require_api_access)])
async def api_v2_learning_restore(proposal_id: str):
    if not _agent.learning_engine.restore_retired(proposal_id):
        raise HTTPException(409, "Artifact is not retired")
    return {"status": "canary", "proposal_id": proposal_id}


@app.get("/api/v2/learning/{proposal_id}/capsule", dependencies=[Depends(require_api_access)])
async def api_v2_learning_export_capsule(proposal_id: str):
    try:
        capsule = _agent.learning_engine.export_capsule(proposal_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if capsule is None:
        raise HTTPException(404, "Learning proposal not found")
    return capsule


@app.post("/api/v2/learning/capsules", dependencies=[Depends(require_api_access)])
async def api_v2_learning_import_capsule(request: Request):
    try:
        return _agent.learning_engine.import_capsule(await request.json())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v2/learning/constitution", dependencies=[Depends(require_api_access)])
async def api_v2_learning_constitution():
    return _agent.learning_engine.constitution()


@app.post("/api/v2/learning/{proposal_id}/rollback", dependencies=[Depends(require_api_access)])
async def api_v2_learning_rollback(proposal_id: str):
    if not _agent.learning_engine.rollback(proposal_id):
        raise HTTPException(404, "Learning proposal not found")
    return {"status": "rolled_back", "proposal_id": proposal_id}


@app.get("/api/v2/events", dependencies=[Depends(require_api_access)])
async def api_v2_events(event_type: str | None = None, session_id: str | None = None, limit: int = 100):
    session = _normalise_session_id(session_id) if session_id else None
    return {"events": _agent.memory_bank.store.list_events(
        event_type=event_type, limit=max(1, min(limit, 500)),
        workspace_id=_agent.memory_bank.workspace_id, session_id=session, user_id=_SERVER_ACTOR_ID,
    )}


@app.get("/api/v2/sessions", dependencies=[Depends(require_api_access)])
async def api_v2_sessions(limit: int = 100):
    return {"sessions": _agent.memory_bank.store.list_sessions(
        workspace_id=_agent.memory_bank.workspace_id, limit=max(1, min(limit, 500)),
        user_id=_SERVER_ACTOR_ID,
    )}


@app.get("/api/v2/sessions/{session_id}", dependencies=[Depends(require_api_access)])
async def api_v2_session(session_id: str):
    session_id = _normalise_session_id(session_id)
    session = _get_or_create_session(session_id, _SERVER_ACTOR_ID)
    return {"session_id": session_id, "messages": session["messages"], "updated": session.get("updated")}


@app.get("/api/v2/schedules", dependencies=[Depends(require_api_access)])
async def api_v2_schedules():
    return {"schedules": _scheduler.list_jobs()}


@app.post("/api/v2/schedules", dependencies=[Depends(require_api_access)])
async def api_v2_create_schedule(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("name", "")).strip():
        raise HTTPException(400, "name is required")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not payload.get("type"):
        raise HTTPException(400, "payload.type is required")
    if "interval_seconds" in body:
        job_id = _scheduler.schedule_every(
            str(body["name"]), float(body["interval_seconds"]), payload=payload,
            job_id=str(body["id"]) if body.get("id") else None,
        )
    elif body.get("run_at"):
        job_id = _scheduler.schedule_once(
            str(body["name"]), str(body["run_at"]), payload=payload,
            job_id=str(body["id"]) if body.get("id") else None,
        )
    else:
        raise HTTPException(400, "interval_seconds or run_at is required")
    return {"schedule": next(item for item in _scheduler.list_jobs() if item["id"] == job_id)}


@app.post("/api/v2/schedules/{job_id}/disable", dependencies=[Depends(require_api_access)])
async def api_v2_disable_schedule(job_id: str):
    if not _agent.memory_bank.store.set_schedule_enabled(job_id, False, workspace_id=_agent.memory_bank.workspace_id,
                                                         user_id=_SERVER_ACTOR_ID):
        raise HTTPException(404, "Schedule not found")
    return {"status": "disabled", "job_id": job_id}


@app.get("/api/v2/skills", dependencies=[Depends(require_api_access)])
async def api_v2_skills(status: str | None = None):
    return {"skills": _agent.skill_registry.list(status)}


@app.post("/api/v2/skills/install", dependencies=[Depends(require_api_access)])
async def api_v2_install_skill(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("path", "")).strip():
        raise HTTPException(400, "path is required")
    try:
        return _agent.skill_registry.install(str(body["path"]), activate=bool(body.get("activate", False)))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v2/skills/{skill_id}/activate", dependencies=[Depends(require_api_access)])
async def api_v2_activate_skill(skill_id: str):
    if not _agent.skill_registry.activate(skill_id):
        raise HTTPException(400, "Skill validation failed or skill not found")
    return {"status": "active", "skill_id": skill_id}


@app.post("/api/v2/skills/{skill_id}/rollback", dependencies=[Depends(require_api_access)])
async def api_v2_rollback_skill(skill_id: str):
    if not _agent.skill_registry.rollback(skill_id):
        raise HTTPException(404, "Skill not found")
    return {"status": "rolled_back", "skill_id": skill_id}


@app.get("/api/v2/agents", dependencies=[Depends(require_api_access)])
async def api_v2_agents():
    return {"agents": _agent.subagent_manager.list_profiles()}


@app.post("/api/v2/agents/run", dependencies=[Depends(require_api_access)])
async def api_v2_run_agent(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("profile", "")).strip() or not str(body.get("task", "")).strip():
        raise HTTPException(400, "profile and task are required")
    try:
        result = await asyncio.to_thread(
            _agent.subagent_manager.run,
            str(body["profile"]), str(body["task"]),
            workspace_id=_agent.memory_bank.workspace_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/cost", dependencies=[Depends(require_api_access)])
async def api_cost():
    """Get cost summary."""
    return {"summary": get_cost_summary()}


@app.get("/api/health", dependencies=[Depends(require_api_access)])
async def api_health():
    """Health check."""
    provider_ok = _agent.llm_provider is not None
    return {
        "status": "ok" if provider_ok else "degraded",
        "provider": _agent._provider_config.provider if _agent._provider_config else "unknown",
        "memory_count": _agent.memory_bank.count_logs(),
    }


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) endpoint
# ---------------------------------------------------------------------------

_MCP_PROTOCOL_VERSION = "2025-06-18"
_MCP_SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-06-18"}
_MCP_SERVER_INFO = {"name": "openkyrozen", "version": app.version}


def _mcp_response(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _mcp_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _mcp_response(request_id, error=error)


def _mcp_tool_schema(name: str, fn: Any) -> dict[str, Any]:
    """Return the object schema for a tool's existing string contract."""
    schemas: dict[str, dict[str, Any]] = {
        "read_file": {
            "properties": {"path": {"type": "string"}}, "required": ["path"],
        },
        "write_file": {
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, "required": ["path", "content"],
        },
        "list_dir": {"properties": {"path": {"type": "string"}}},
        "list_tree": {"properties": {"path": {"type": "string"}}},
        "find_files": {
            "properties": {
                "pattern": {"type": "string"},
                "directory": {"type": "string"},
            }, "required": ["pattern"],
        },
        "run_cmd": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
        "execute_terminal_command": {
            "properties": {"command": {"type": "string"}}, "required": ["command"],
        },
        "search_web": {"properties": {"query": {"type": "string"}}, "required": ["query"]},
        "read_webpage": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "git_clone": {
            "properties": {
                "url": {"type": "string"},
                "destination": {"type": "string"},
            }, "required": ["url"],
        },
        "analyze_remote_repo": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "browser_open": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "browser_snapshot": {"properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        "browser_close": {"properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        "browser_click": {
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
            }, "required": ["session_id", "selector"],
        },
        "browser_type": {
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
            }, "required": ["session_id", "selector", "text"],
        },
    }
    schema = copy.deepcopy(schemas.get(name, {
        "properties": {"args": {"type": "string"}},
    }))
    schema["type"] = "object"
    schema["additionalProperties"] = False
    if name not in schemas:
        schema["description"] = "Plain-string arguments can be supplied as the `args` property."
    return schema


def _mcp_tool_descriptors(allowed: set[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": (getattr(fn, "__doc__", "") or "").strip().split("\n")[0],
            "inputSchema": _mcp_tool_schema(name, fn),
        }
        for name, fn in _agent.AVAILABLE_TOOLS.items()
        if name in allowed
    ]


def _mcp_string_arguments(tool_name: str, arguments: Any) -> str:
    """Map MCP object arguments to the tool's documented pipe/string format."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object or plain string")
    if not arguments:
        return ""
    if set(arguments) == {"args"}:
        if not isinstance(arguments["args"], str):
            raise ValueError("arguments.args must be a string")
        return arguments["args"]

    def value(*names: str, required: bool = True) -> str:
        present = next((name for name in names if name in arguments), None)
        if present is None:
            if required:
                raise ValueError(f"missing required argument: {names[0]}")
            return ""
        if not isinstance(arguments[present], str):
            raise ValueError(f"argument '{present}' must be a string")
        return arguments[present]

    if tool_name == "read_file":
        return value("path", "file_path")
    if tool_name == "write_file":
        return f"{value('path', 'file_path')}|{value('content', 'text')}"
    if tool_name in {"list_dir", "list_tree"}:
        return value("path", required=False)
    if tool_name == "find_files":
        pattern = value("pattern")
        directory = value("directory", required=False)
        return f"{pattern}|{directory}" if directory else pattern
    if tool_name in {"run_cmd", "execute_terminal_command"}:
        return value("command", "cmd")
    if tool_name == "search_web":
        return value("query")
    if tool_name in {"read_webpage", "browser_open", "analyze_remote_repo"}:
        return value("url")
    if tool_name == "git_clone":
        url = value("url")
        destination = value("destination", required=False)
        return f"{url}|{destination}" if destination else url
    if tool_name in {"browser_snapshot", "browser_close"}:
        return value("session_id", "sessionId")
    if tool_name == "browser_click":
        return f"{value('session_id', 'sessionId')}|{value('selector')}"
    if tool_name == "browser_type":
        return f"{value('session_id', 'sessionId')}|{value('selector')}|{value('text')}"

    # Every legacy tool has a plain-string contract.  Requiring the explicit
    # `args` wrapper keeps ambiguous object shapes from silently changing the
    # command sent to a tool.
    raise ValueError(f"tool '{tool_name}' accepts object arguments only as {{'args': '<string>'}}")


@app.post("/mcp", dependencies=[Depends(require_api_access)])
async def mcp_endpoint(request: Request):
    """MCP-compatible endpoint for AI tool interoperability."""
    try:
        body = await request.json()
    except Exception:
        return _mcp_error(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return _mcp_error(None, -32600, "Invalid Request")
    request_id = body.get("id")
    if "id" in body and isinstance(request_id, (dict, list, bool)):
        request_id = None
    if body.get("jsonrpc") not in (None, "2.0") or not isinstance(body.get("method"), str):
        return _mcp_error(request_id, -32600, "Invalid Request")
    method = body["method"]
    params = body.get("params", {})
    if not isinstance(params, dict):
        return _mcp_error(request_id, -32602, "Invalid params: params must be an object")

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        selected_version = (
            requested_version if requested_version in _MCP_SUPPORTED_PROTOCOL_VERSIONS
            else _MCP_PROTOCOL_VERSION
        )
        return _mcp_response(request_id, result={
            "protocolVersion": selected_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _MCP_SERVER_INFO,
        })
    if method in {"notifications/initialized", "ping"}:
        return _mcp_response(request_id, result={})
    if method == "server/discover":
        allowed = _allowed_server_tools("mcp")
        return _mcp_response(request_id, result={
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "serverInfo": _MCP_SERVER_INFO,
            "capabilities": {"tools": {"listChanged": False}},
            "tools": _mcp_tool_descriptors(allowed),
        })
    if method == "tools/list":
        allowed = _allowed_server_tools("mcp")
        return _mcp_response(request_id, result={"tools": _mcp_tool_descriptors(allowed)})
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", "")
        if not isinstance(tool_name, str) or not tool_name:
            _agent._notify_tool_execute(str(tool_name), tool_args, "Error: tool name is required")
            return _mcp_error(request_id, -32602, "Invalid params: tool name is required")
        fn = _agent.AVAILABLE_TOOLS.get(tool_name)
        if fn is None:
            _agent._notify_tool_execute(tool_name, tool_args, f"Error: Tool '{tool_name}' not found")
            return _mcp_error(request_id, -32601, f"Tool '{tool_name}' not found")
        if tool_name not in _allowed_server_tools("mcp"):
            _agent._notify_tool_execute(
                tool_name, tool_args, f"Error: Tool '{tool_name}' is not authorized",
            )
            return _mcp_error(request_id, -32001, (
                f"Tool requires '{tool_capability(tool_name)}' capability; "
                "set KYROZEN_MCP_CAPABILITIES or use the full profile to enable it"
            ))
        try:
            string_args = _mcp_string_arguments(tool_name, tool_args)
        except (TypeError, ValueError) as e:
            _agent._notify_tool_execute(tool_name, tool_args, f"Error: invalid params: {e}")
            return _mcp_error(request_id, -32602, f"Invalid params: {e}")
        try:
            previous_token = _agent._execution_capability_token
            _agent._execution_capability_token = issue_capability_token(
                "surface:mcp", _server_capabilities("mcp"), ttl_seconds=300,
            )
            try:
                result = _agent._run_tool(tool_name, string_args)
            finally:
                _agent._execution_capability_token = previous_token
            result_text = str(result)
        except Exception as e:
            result_text = f"Error: {e}"
        return _mcp_response(request_id, result={
            "content": [{"type": "text", "text": result_text}],
            "isError": _agent._is_tool_error(result_text),
        })
    if method == "chat/send":
        try:
            msg = _validate_message(_sanitize_api_message(str(params.get("message", "")).strip()))
        except HTTPException as exc:
            return _mcp_error(request_id, -32602, str(exc.detail))
        if not msg:
            return _mcp_error(request_id, -32602, "Empty message")
        session_id = _normalise_session_id(params.get("session_id"))
        session = _get_or_create_session(session_id, _SERVER_ACTOR_ID)
        reply = _run_session_chat(session, msg)
        return _mcp_response(request_id, result={"content": reply, "session_id": session_id})
    return _mcp_error(request_id, -32601, f"Unknown method: {method}")


# ---------------------------------------------------------------------------
# Voice endpoints (TTS / STT)
# ---------------------------------------------------------------------------

@app.post("/api/voice/transcribe", dependencies=[Depends(require_api_access)])
async def api_transcribe(request: Request):
    """Transcribe audio to text using system tools (macOS say, Linux espeak)."""
    # This is a placeholder — real STT would use whisper or an API
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    text = body.get("text", "")
    return {"text": text, "source": "passthrough", "note": "Use whisper or cloud STT for real transcription"}


@app.get("/api/voice/speak", dependencies=[Depends(require_api_access)])
async def api_speak(text: str = ""):
    """Text-to-speech using system TTS tools."""
    if not text:
        return {"error": "No text provided"}
    import subprocess, platform
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["say", text])
        elif system == "Linux":
            subprocess.Popen(["espeak", text])
        elif system == "Windows":
            # Keep user text in the environment rather than interpolating it
            # into PowerShell source code.
            env = os.environ.copy()
            env["KYROZEN_TTS_TEXT"] = text
            subprocess.Popen([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName System.Speech; "
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Speak($env:KYROZEN_TTS_TEXT)",
            ], env=env)
        return {"status": "speaking", "text": text[:100]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Webhook system
# ---------------------------------------------------------------------------

_webhooks: list[dict] = []
_MAX_WEBHOOKS = 32
_ALLOWED_WEBHOOK_EVENTS = {"chat.completed", "test"}
_WEBHOOK_SUMMARY_CHARS = 500


def _redact_webhook_value(value: Any, limit: int = _WEBHOOK_SUMMARY_CHARS) -> str:
    """Bound and redact values before they leave the server process."""
    text = str(value or "")
    patterns = (
        (r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=])\s*\S+", r"\1<redacted>"),
        (r"\bsk-[A-Za-z0-9_-]+", "<redacted>"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text.replace("\n", " ")[:limit]


def _chat_completed_payload(session: dict[str, Any], reply: str, *, streamed: bool) -> dict[str, Any]:
    """Build the documented, minimal payload for a completed chat."""
    return {
        "actor": _redact_webhook_value(session.get("user_id", _SERVER_ACTOR_ID), 64),
        "session_id": _redact_webhook_value(session.get("session_id", ""), 128),
        "profile": _redact_webhook_value(session.get("profile", "auto"), 32),
        "reply_summary": _redact_webhook_value(reply),
        "reply_length": len(str(reply)),
        "streamed": bool(streamed),
    }


def _emit_chat_completed(session: dict[str, Any], reply: str, *, streamed: bool) -> None:
    """Emit one completion event without making webhook delivery part of chat."""
    try:
        _fire_webhooks("chat.completed", _chat_completed_payload(
            session, reply, streamed=streamed,
        ))
    except Exception as exc:
        # Keep a defensive boundary around custom/test senders as well as the
        # normal requests-based sender.
        _audit("WEBHOOK_FAILURE", f"event=chat.completed error={type(exc).__name__}: {exc}")


def _validate_webhook_url(url: str) -> bool:
    """Reject malformed and obvious local/private webhook destinations."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False
    except ValueError:
        # DNS rebinding cannot be fully solved in this in-process implementation;
        # production deployments should use an egress proxy as well.
        pass
    return len(url) <= 2048

@app.post("/api/webhooks/register", dependencies=[Depends(require_api_access)])
async def register_webhook(request: Request):
    """Register a webhook URL for event notifications."""
    body = await request.json()
    url = body.get("url", "").strip()
    events = body.get("events", ["chat.completed"])
    if not url:
        raise HTTPException(400, "URL required")
    if len(_webhooks) >= _MAX_WEBHOOKS:
        raise HTTPException(429, "Webhook limit reached")
    if not _validate_webhook_url(url):
        raise HTTPException(400, "Only public http(s) webhook URLs are allowed")
    if not isinstance(events, list) or not events or any(
        not isinstance(event, str) or event not in _ALLOWED_WEBHOOK_EVENTS for event in events
    ):
        raise HTTPException(400, "Unsupported webhook event")
    hook = {"url": url, "events": events, "created": time.time()}
    _webhooks.append(hook)
    _audit("WEBHOOK_REGISTER", f"url={url} events={events}")
    return {"status": "registered", "id": len(_webhooks) - 1}


@app.get("/api/webhooks", dependencies=[Depends(require_api_access)])
async def list_webhooks():
    """List registered webhooks."""
    return {"webhooks": _webhooks}


def _fire_webhooks(event: str, data: dict):
    """Fire webhooks and audit delivery failures without raising to callers."""
    try:
        import requests as _req
    except Exception as exc:
        _audit("WEBHOOK_FAILURE", f"event={event} error={type(exc).__name__}: {exc}")
        return {"sent": 0, "failed": 0}
    sent = failed = 0
    for hook in tuple(_webhooks):
        if event in hook["events"]:
            try:
                response = _req.post(hook["url"], json={"event": event, "data": data}, timeout=5)
                status_code = getattr(response, "status_code", 200)
                if status_code >= 400:
                    raise RuntimeError(f"HTTP {status_code}")
                sent += 1
            except Exception as exc:
                failed += 1
                _audit(
                    "WEBHOOK_FAILURE",
                    f"event={event} url={_redact_webhook_value(hook.get('url', ''), 256)} "
                    f"error={type(exc).__name__}: {exc}",
                )
    return {"sent": sent, "failed": failed}


@app.post("/api/webhooks/test", dependencies=[Depends(require_api_access)])
async def test_webhook():
    """Manually trigger a test webhook event."""
    _fire_webhooks("test", {"message": "Webhook test from OpenKyrozen"})
    return {"status": "fired", "hook_count": len(_webhooks)}


# PWA manifest
@app.get("/manifest.json")
async def pwa_manifest():
    return {
        "name": "OpenKyrozen",
        "short_name": "Kyrozen",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#00f0ff",
        "icons": [{"src": "/static/icon.png", "sizes": "192x192", "type": "image/png"}]
    }


# Async sleep helper
async def asyncio_sleep(seconds: float):
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Plugin system
# ---------------------------------------------------------------------------

def _load_plugins():
    """Load the shared Web plugin runtime exactly once."""
    return _agent._plugin_runtime_for_surface("web").load_once()


def _trigger_hook(hook_name: str, **kwargs):
    """Compatibility wrapper for startup hooks through the shared runtime."""
    return _agent._plugin_runtime_for_surface("web").trigger(hook_name, **kwargs)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Initialize the agent on server start."""
    print("[Server] Initializing OpenKyrozen...")
    if _agent.get_launch_context() is None:
        _agent.configure_launch_context()
    context = _agent.get_launch_context()
    if context is not None:
        print(f"[Server] {context.describe()}")
    _agent._prompt_and_init_deepseek(interactive=False)
    if _agent.llm_provider is None:
        print("[Server] WARNING: No LLM provider configured. Set API key env vars.")
    else:
        print(f"[Server] Provider: {_agent._provider_config.provider}")
    _agent._load_project_files_into_memory()
    _load_plugins()
    _trigger_hook(
        "on_startup", agent=_agent, surface="web", user_id=_SERVER_ACTOR_ID,
        workspace_id=_agent.memory_bank.workspace_id,
    )
    recovered = _recover_task_scopes()
    if recovered:
        _audit("TASK_RECOVERY", f"recovered={len(recovered)} pending_or_resumable tasks")
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in _scheduler.list_jobs()):
        _scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    if not any(job.get("payload", {}).get("type") == "learning_cycle" for job in _scheduler.list_jobs()):
        _scheduler.schedule_every("learning-cycle", 30.0, payload={"type": "learning_cycle"},
                                  job_id="job_learning_cycle", delay_seconds=0)
    _scheduler.start()
    _audit("STARTUP", "server started")
    print("[Server] Ready — http://127.0.0.1:8000 (set KYROZEN_SERVER_TOKEN for remote access)")


@app.on_event("shutdown")
async def shutdown():
    _scheduler.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _server_parser():
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


def _parse_server_args(argv: list[str] | None = None):
    args = _server_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        context = _agent.configure_launch_context(
            project_path=args.project,
            global_mode=args.global_mode,
        )
    except ValueError as exc:
        _server_parser().error(str(exc))
    return args, context


def main_entry():
    """Entry point for pyproject.toml console_scripts."""
    args, context = _parse_server_args()
    if args.reload:
        # Uvicorn imports the app in a child process when reloading. Carry the
        # already-resolved mode across that import without using process cwd.
        os.environ["KYROZEN_WORKSPACE_ROOT"] = str(context.active_root)
        os.environ["KYROZEN_LAUNCH_MODE"] = context.mode
        uvicorn.run(
            "server:app", host=args.host, port=args.port, reload=True,
            app_dir=str(Path(__file__).resolve().parent),
        )
        return
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main_entry()
