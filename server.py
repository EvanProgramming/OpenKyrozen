"""
OpenKyrozen Web Server — FastAPI + Chat UI + REST API
Run: python server.py  or  uvicorn server:app --host 0.0.0.0 --port 8000
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
from task_engine import TaskManager
from scheduler import JobScheduler
from tools import allowed_tool_names, resolve_capabilities, tool_capability

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
_audit_log_path = Path("kyrozen_audit.log")

def _audit(event: str, detail: str = "", user: str = "anonymous") -> None:
    """Append an audit entry to the log file."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(_audit_log_path, "a") as f:
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
_scheduler = JobScheduler(_agent.memory_bank.store, workspace_id=_agent.memory_bank.workspace_id)


def _run_scheduled_job(job: dict[str, Any]) -> None:
    payload = job.get("payload", {})
    if payload.get("type") == "learning_cycle":
        if _agent._SELF_LEARNING_FLAGS.get("auto_learn_conversations", True):
            _agent._auto_learn_conversations()
        if _agent._SELF_LEARNING_FLAGS.get("outcome_verified_evolution", True):
            _agent._review_evolution_runs()
        return
    if payload.get("type") == "chat":
        session_id = _normalise_session_id(payload.get("session_id"))
        session = _get_or_create_session(session_id, "scheduler")
        _run_session_chat(session, str(payload.get("message", "")))
        return
    raise ValueError(f"Unknown scheduled job type: {payload.get('type', 'default')}")


_scheduler.register_callback("learning_cycle", _run_scheduled_job)
_scheduler.register_callback("chat", _run_scheduled_job)


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


def _actor_for_request(request: Request) -> str:
    """Return a coarse audit actor; user_id is never accepted from JSON."""
    return "authenticated" if _SERVER_TOKEN else "loopback"


def _get_or_create_session(session_id: str, user_id: str = "anonymous") -> dict:
    with _chat_lock:
        if session_id not in _sessions:
            persisted = _agent.memory_bank.store.list_events(
                event_type="session.message", limit=_MAX_SESSION_MESSAGES,
                workspace_id=_agent.memory_bank.workspace_id, session_id=session_id,
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
            reply = (_agent._chat_turn(message, clear_tasks=True, profile=profile)
                     if profile != "auto" else _agent._chat_turn(message, clear_tasks=True))
            session["last_learning_run"] = _agent._last_learning_run
            _agent.short_term_memory.extend([
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ])
            session["messages"] = _agent.short_term_memory[-_MAX_SESSION_MESSAGES:]
            session["updated"] = time.time()
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
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let full = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') continue;
          try {
            const parsed = JSON.parse(data);
            if (parsed.chunk) { full += parsed.chunk; contentDiv.textContent = full; }
            if (parsed.cost) document.getElementById('cost-display').textContent = parsed.cost;
            if (parsed.error) { contentDiv.textContent = 'Error: ' + parsed.error; contentDiv.classList.add('error'); }
          } catch(e) {}
        }
      }
      document.getElementById('chat').scrollTop = document.getElementById('chat').scrollHeight;
    }
    document.getElementById('status').textContent = 'Ready';
  } catch(e) {
    contentDiv.textContent = 'Connection error: ' + e.message;
    contentDiv.classList.add('error');
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
    _audit("CHAT", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    try:
        reply = _run_session_chat(session, msg)
    except Exception as e:
        _audit("ERROR", str(e), session["user_id"])
        raise HTTPException(500, str(e))

    _audit("REPLY", f"len={len(reply)}", session["user_id"])
    return {"reply": reply, "session_id": session_id, "profile": session["profile"], "cost": get_cost_summary()}


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
            yield "data: [DONE]\n\n"
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
async def api_v2_memory(q: str = "", limit: int = 10, session_id: str | None = None):
    """Structured memory endpoint with scope and provenance metadata."""
    limit = max(1, min(limit, _MAX_MEMORY_RESULTS))
    if q:
        memory = MemoryBank(_agent.memory_bank.db_path, workspace_id=_agent.memory_bank.workspace_id,
                            session_id=_normalise_session_id(session_id) if session_id else None)
        results = memory.recall_records(q, n_results=limit)
    else:
        memory = MemoryBank(_agent.memory_bank.db_path, workspace_id=_agent.memory_bank.workspace_id,
                            session_id=_normalise_session_id(session_id) if session_id else None)
        results = memory.store.list_memories(status="active", limit=limit,
                                             workspace_id=memory.workspace_id, session_id=memory.session_id)
    return {"results": results, "total": memory.count_logs(), "scope": {
        "workspace_id": memory.workspace_id, "session_id": memory.session_id,
    }}


@app.get("/api/v2/tasks", dependencies=[Depends(require_api_access)])
async def api_v2_tasks(session_id: str | None = None):
    session = _normalise_session_id(session_id) if session_id else None
    manager = TaskManager(_agent.memory_bank.store, workspace_id=_agent.memory_bank.workspace_id, session_id=session)
    return {"tasks": manager.store.list_tasks(workspace_id=manager.workspace_id, session_id=session)}


@app.post("/api/v2/tasks", dependencies=[Depends(require_api_access)])
async def api_v2_create_task(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("description", "")).strip():
        raise HTTPException(400, "description is required")
    session = _normalise_session_id(body.get("session_id"))
    manager = TaskManager(_agent.memory_bank.store, workspace_id=_agent.memory_bank.workspace_id, session_id=session)
    index = manager.add_task(
        str(body["description"]),
        dependencies=body.get("dependencies") if isinstance(body.get("dependencies"), list) else None,
        acceptance=body.get("acceptance") if isinstance(body.get("acceptance"), list) else None,
        priority=int(body.get("priority", 0)),
    )
    return {"task": manager.tasks[index]}


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
        workspace_id=_agent.memory_bank.workspace_id, session_id=session,
    )}


@app.get("/api/v2/sessions", dependencies=[Depends(require_api_access)])
async def api_v2_sessions(limit: int = 100):
    return {"sessions": _agent.memory_bank.store.list_sessions(
        workspace_id=_agent.memory_bank.workspace_id, limit=max(1, min(limit, 500)),
    )}


@app.get("/api/v2/sessions/{session_id}", dependencies=[Depends(require_api_access)])
async def api_v2_session(session_id: str):
    session_id = _normalise_session_id(session_id)
    session = _get_or_create_session(session_id, "authenticated")
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
    if not _agent.memory_bank.store.set_schedule_enabled(job_id, False, workspace_id=_agent.memory_bank.workspace_id):
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

@app.post("/mcp", dependencies=[Depends(require_api_access)])
async def mcp_endpoint(request: Request):
    """MCP-compatible endpoint for AI tool interoperability."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    method = body.get("method", "")
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(400, "params object required")

    if method == "tools/list":
        allowed = _allowed_server_tools("mcp")
        return {
            "jsonrpc": "2.0",
            "result": {
                "tools": [
                    {"name": name, "description": (fn.__doc__ or "").strip().split("\n")[0]}
                    for name, fn in _agent.AVAILABLE_TOOLS.items()
                    if name in allowed
                ]
            }
        }
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", "")
        fn = _agent.AVAILABLE_TOOLS.get(tool_name)
        if fn is None:
            return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"}}
        if tool_name not in _allowed_server_tools("mcp"):
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        f"Tool requires '{tool_capability(tool_name)}' capability; "
                        "set KYROZEN_MCP_CAPABILITIES or use the full profile to enable it"
                    ),
                },
            }
        try:
            result = fn(str(tool_args))
            return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": str(result)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
    elif method == "chat/send":
        msg = _validate_message(_sanitize_api_message(str(params.get("message", "")).strip()))
        if not msg:
            raise HTTPException(400, "Empty message")
        session_id = _normalise_session_id(params.get("session_id"))
        session = _get_or_create_session(session_id, "authenticated")
        reply = _run_session_chat(session, msg)
        return {"jsonrpc": "2.0", "result": {"content": reply, "session_id": session_id}}
    else:
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown method: {method}"}}


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
    """Fire webhooks for a given event."""
    import requests as _req
    for hook in _webhooks:
        if event in hook["events"]:
            try:
                _req.post(hook["url"], json={"event": event, "data": data}, timeout=5)
            except Exception:
                pass


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

_PLUGINS: list[Any] = []

def _load_plugins():
    """Load plugins from the plugins/ directory."""
    global _PLUGINS
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.is_dir():
        plugins_dir.mkdir(exist_ok=True)
        (plugins_dir / "__init__.py").write_text("# OpenKyrozen plugins\n")
        return
    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = __import__(f"plugins.{py_file.stem}", fromlist=["register"])
            if hasattr(spec, "register"):
                plugin = spec.register()
                _PLUGINS.append(plugin)
                print(f"[Plugin] Loaded: {py_file.stem}")
        except Exception as e:
            print(f"[Plugin] Failed to load {py_file.stem}: {e}")


# Hook triggers
def _trigger_hook(hook_name: str, **kwargs):
    for plugin in _PLUGINS:
        fn = getattr(plugin, hook_name, None)
        if fn:
            try:
                fn(**kwargs)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    """Initialize the agent on server start."""
    print("[Server] Initializing OpenKyrozen...")
    _agent._set_workspace_root(os.getcwd())
    _agent._prompt_and_init_deepseek()
    if _agent.llm_provider is None:
        print("[Server] WARNING: No LLM provider configured. Set API key env vars.")
    else:
        print(f"[Server] Provider: {_agent._provider_config.provider}")
    _agent._load_project_files_into_memory()
    _load_plugins()
    _trigger_hook("on_startup", agent=_agent)
    _scheduler.start()
    _audit("STARTUP", "server started")
    print("[Server] Ready — http://127.0.0.1:8000 (set KYROZEN_SERVER_TOKEN for remote access)")


@app.on_event("shutdown")
async def shutdown():
    _scheduler.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenKyrozen Web Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


def main_entry():
    """Entry point for pyproject.toml console_scripts."""
    uvicorn.run("server:app", host="127.0.0.1", port=8000)
