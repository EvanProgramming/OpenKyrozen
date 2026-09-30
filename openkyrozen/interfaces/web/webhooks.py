from __future__ import annotations

import time
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _redact_webhook_value(self, value: Any, limit: int = 500) -> str:
    service = self
    """Bound and redact values before they leave the server process."""
    text = str(value or "")
    patterns = (
        (r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=])\s*\S+", r"\1<redacted>"),
        (r"\bsk-[A-Za-z0-9_-]+", "<redacted>"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text.replace("\n", " ")[:limit]


def _chat_completed_payload(self, session: dict[str, Any], reply: str, *, streamed: bool) -> dict[str, Any]:
    service = self
    """Build the documented, minimal payload for a completed chat."""
    return {
        "actor": service._redact_webhook_value(session.get("user_id", service._SERVER_ACTOR_ID), 64),
        "session_id": service._redact_webhook_value(session.get("session_id", ""), 128),
        "profile": service._redact_webhook_value(session.get("profile", "auto"), 32),
        "reply_summary": service._redact_webhook_value(reply),
        "reply_length": len(str(reply)),
        "streamed": bool(streamed),
    }


def _emit_chat_completed(self, session: dict[str, Any], reply: str, *, streamed: bool) -> None:
    service = self
    """Emit one completion event without making webhook delivery part of chat."""
    try:
        service._fire_webhooks("chat.completed", service._chat_completed_payload(
            session, reply, streamed=streamed,
        ))
    except Exception as exc:
        # Keep a defensive boundary around custom/test senders as well as the
        # normal requests-based sender.
        service._audit("WEBHOOK_FAILURE", f"event=chat.completed error={type(exc).__name__}: {exc}")


def _validate_webhook_url(self, url: str) -> bool:
    service = self
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


async def register_webhook(self, request: Request):
    service = self
    """Register a webhook URL for event notifications."""
    body = await service._json_object(request)
    if "url" in body and not isinstance(body["url"], str):
        raise HTTPException(400, "URL required")
    url = body.get("url", "").strip()
    events = body.get("events", ["chat.completed"])
    if not url:
        raise HTTPException(400, "URL required")
    if len(service._webhooks) >= service._MAX_WEBHOOKS:
        raise HTTPException(429, "Webhook limit reached")
    if not service._validate_webhook_url(url):
        raise HTTPException(400, "Only public http(s) webhook URLs are allowed")
    if not isinstance(events, list) or not events or any(
        not isinstance(event, str) or event not in service._ALLOWED_WEBHOOK_EVENTS for event in events
    ):
        raise HTTPException(400, "Unsupported webhook event")
    hook = {"url": url, "events": events, "created": time.time()}
    service._webhooks.append(hook)
    service._audit("WEBHOOK_REGISTER", f"url={url} events={events}")
    return {"status": "registered", "id": len(service._webhooks) - 1}


async def list_webhooks(self):
    service = self
    """List registered webhooks."""
    return {"webhooks": service._webhooks}


def _fire_webhooks(self, event: str, data: dict):
    service = self
    """Fire webhooks and audit delivery failures without raising to callers."""
    try:
        import requests as _req
    except Exception as exc:
        service._audit("WEBHOOK_FAILURE", f"event={event} error={type(exc).__name__}: {exc}")
        return {"sent": 0, "failed": 0}
    sent = failed = 0
    for hook in tuple(service._webhooks):
        if event in hook["events"]:
            try:
                response = _req.post(hook["url"], json={"event": event, "data": data}, timeout=5)
                status_code = getattr(response, "status_code", 200)
                if status_code >= 400:
                    raise RuntimeError(f"HTTP {status_code}")
                sent += 1
            except Exception as exc:
                failed += 1
                service._audit(
                    "WEBHOOK_FAILURE",
                    f"event={event} url={service._redact_webhook_value(hook.get('url', ''), 256)} "
                    f"error={type(exc).__name__}: {exc}",
                )
    return {"sent": sent, "failed": failed}


async def test_webhook(self):
    service = self
    """Manually trigger a test webhook event."""
    service._fire_webhooks("test", {"message": "Webhook test from OpenKyrozen"})
    return {"status": "fired", "hook_count": len(service._webhooks)}
