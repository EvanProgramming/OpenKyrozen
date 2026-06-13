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
from pathlib import Path
from typing import Any

# Add parent to path so we can import main
sys.path.insert(0, str(Path(__file__).parent))

# Minimal env setup for headless mode
os.environ.setdefault("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))

import main as _agent
from providers import get_cost_summary, reset_cost_tracker
from memory import MemoryBank

try:
    from fastapi import FastAPI, Request, HTTPException
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
    version="1.0.0",
)

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

def _get_or_create_session(session_id: str, user_id: str = "anonymous") -> dict:
    if session_id not in _sessions:
        _sessions[session_id] = {
            "messages": [],
            "user_id": user_id,
            "created": time.time(),
        }
        # Keep only last 100 sessions
        if len(_sessions) > 100:
            oldest = min(_sessions, key=lambda k: _sessions[k]["created"])
            del _sessions[oldest]
    return _sessions[session_id]

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


@app.post("/api/chat")
async def api_chat(request: Request):
    """Non-streaming chat endpoint."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    session = _get_or_create_session(body.get("session_id", "default"), body.get("user_id", "anonymous"))
    _audit("CHAT", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    try:
        reply = _agent._chat_turn(msg, clear_tasks=True)
    except Exception as e:
        _audit("ERROR", str(e), session["user_id"])
        raise HTTPException(500, str(e))

    _audit("REPLY", f"len={len(reply)}", session["user_id"])
    return {"reply": reply, "cost": get_cost_summary()}


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """SSE streaming chat endpoint."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(400, "Empty message")
    session = _get_or_create_session(body.get("session_id", "default"), body.get("user_id", "anonymous"))
    _audit("CHAT_STREAM", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    async def generate():
        try:
            reply = _agent._chat_turn(msg, clear_tasks=True)
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


@app.get("/api/memory")
async def api_memory(q: str = "", limit: int = 10):
    """Search stored memories."""
    if q:
        results = _agent.memory_bank.recall(q, n_results=limit)
    else:
        results = _agent.memory_bank.get_recent(limit)
    return {"results": results, "total": _agent.memory_bank.count_logs()}


@app.get("/api/cost")
async def api_cost():
    """Get cost summary."""
    return {"summary": get_cost_summary()}


@app.get("/api/health")
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

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP-compatible endpoint for AI tool interoperability."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "result": {
                "tools": [
                    {"name": name, "description": (fn.__doc__ or "").strip().split("\n")[0]}
                    for name, fn in _agent.AVAILABLE_TOOLS.items()
                ]
            }
        }
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", "")
        fn = _agent.AVAILABLE_TOOLS.get(tool_name)
        if fn is None:
            return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Tool '{tool_name}' not found"}}
        try:
            result = fn(str(tool_args))
            return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": str(result)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
    elif method == "chat/send":
        msg = params.get("message", "")
        if not msg:
            raise HTTPException(400, "Empty message")
        reply = _agent._chat_turn(msg, clear_tasks=True)
        return {"jsonrpc": "2.0", "result": {"content": reply}}
    else:
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown method: {method}"}}


# ---------------------------------------------------------------------------
# Voice endpoints (TTS / STT)
# ---------------------------------------------------------------------------

@app.post("/api/voice/transcribe")
async def api_transcribe(request: Request):
    """Transcribe audio to text using system tools (macOS say, Linux espeak)."""
    # This is a placeholder — real STT would use whisper or an API
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    text = body.get("text", "")
    return {"text": text, "source": "passthrough", "note": "Use whisper or cloud STT for real transcription"}


@app.get("/api/voice/speak")
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
            subprocess.Popen(["powershell", "-c", f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"])
        return {"status": "speaking", "text": text[:100]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Webhook system
# ---------------------------------------------------------------------------

_webhooks: list[dict] = []

@app.post("/api/webhooks/register")
async def register_webhook(request: Request):
    """Register a webhook URL for event notifications."""
    body = await request.json()
    url = body.get("url", "").strip()
    events = body.get("events", ["chat.completed"])
    if not url:
        raise HTTPException(400, "URL required")
    hook = {"url": url, "events": events, "created": time.time()}
    _webhooks.append(hook)
    _audit("WEBHOOK_REGISTER", f"url={url} events={events}")
    return {"status": "registered", "id": len(_webhooks) - 1}


@app.get("/api/webhooks")
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


@app.post("/api/webhooks/test")
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
import asyncio as _asyncio
async def asyncio_sleep(seconds: float):
    await _asyncio.sleep(seconds)


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
    _agent._prompt_and_init_deepseek()
    if _agent.llm_provider is None:
        print("[Server] WARNING: No LLM provider configured. Set API key env vars.")
    else:
        print(f"[Server] Provider: {_agent._provider_config.provider}")
    _agent._load_project_files_into_memory()
    _load_plugins()
    _trigger_hook("on_startup", agent=_agent)
    _audit("STARTUP", "server started")
    print("[Server] Ready — http://0.0.0.0:8000")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenKyrozen Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)


def main_entry():
    """Entry point for pyproject.toml console_scripts."""
    uvicorn.run("server:app", host="0.0.0.0", port=8000)
