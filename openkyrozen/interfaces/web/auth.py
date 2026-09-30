from __future__ import annotations

import os
import time
import hmac
import ipaddress
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _issue_browser_auth_session(self) -> str:
    service = self
    """Issue a short-lived, HttpOnly browser session without retaining the server token."""
    now = time.time()
    value = secrets.token_urlsafe(32)
    with service._browser_auth_lock:
        expired = [key for key, deadline in service._browser_auth_sessions.items() if deadline <= now]
        for key in expired:
            service._browser_auth_sessions.pop(key, None)
        service._browser_auth_sessions[value] = now + service._BROWSER_AUTH_TTL_SECONDS
    return value


def _browser_auth_session_valid(self, request: Request) -> bool:
    service = self
    value = request.cookies.get(service._BROWSER_AUTH_COOKIE, "")
    if not value:
        return False
    now = time.time()
    with service._browser_auth_lock:
        deadline = service._browser_auth_sessions.get(value)
        if deadline is None:
            return False
        if deadline <= now:
            service._browser_auth_sessions.pop(value, None)
            return False
        return True


def _is_loopback_client(self, request: Request) -> bool:
    service = self
    """Return True only for a direct loopback/test client connection."""
    host = request.client.host if request.client else ""
    if host in service._LOCAL_CLIENT_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_api_access(self, request: Request) -> None:
    service = self
    """Protect API/MCP routes with a bearer token or direct loopback access."""
    if service._SERVER_TOKEN:
        supplied = request.headers.get("authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = request.headers.get("x-kyrozen-token", "").strip()
        if hmac.compare_digest(supplied, service._SERVER_TOKEN):
            return
        if service._browser_auth_session_valid(request):
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if service._is_loopback_client(request):
        return
    raise HTTPException(
        status_code=503,
        detail="Set KYROZEN_SERVER_TOKEN before exposing the server beyond localhost",
    )


def _sanitize_api_message(self, message: str) -> str:
    service = self
    """Apply the same prompt-injection filter to headless API requests."""
    sanitized, flagged = service._agent._sanitize_input(message)
    if flagged:
        service._audit("PROMPT_INJECTION_FILTERED", "API message contained a known pattern")
    return sanitized


def _audit_log_path(self) -> Path:
    service = self
    explicit = os.environ.get("KYROZEN_AUDIT_LOG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return service._agent._state_root() / "kyrozen_audit.log"


def _audit(self, event: str, detail: str = "", user: str = "anonymous") -> None:
    service = self
    """Append an audit entry to the log file."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        audit_path = service._audit_log_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write(f"[{ts}] [{user}] {event} | {detail}\n")
    except Exception:
        pass


async def api_auth_session(self, request: Request):
    service = self
    """Exchange a user-entered server token for an HttpOnly browser session."""
    if not service._SERVER_TOKEN:
        return {"authenticated": True}
    body = await service._json_object(request)
    supplied = str(body.get("token", "")).strip()
    if not hmac.compare_digest(supplied, service._SERVER_TOKEN):
        service._audit("AUTH_FAILURE", "browser bootstrap rejected")
        response = JSONResponse({"detail": "Invalid server token"}, status_code=401)
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    cookie = service._issue_browser_auth_session()
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        service._BROWSER_AUTH_COOKIE, cookie, max_age=service._BROWSER_AUTH_TTL_SECONDS,
        httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/",
    )
    return response


async def api_auth_session_logout(self, request: Request):
    service = self
    """Revoke the current in-memory browser session and clear its cookie."""
    value = request.cookies.get(service._BROWSER_AUTH_COOKIE, "")
    if value:
        with service._browser_auth_lock:
            service._browser_auth_sessions.pop(value, None)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(service._BROWSER_AUTH_COOKIE, path="/")
    return response
