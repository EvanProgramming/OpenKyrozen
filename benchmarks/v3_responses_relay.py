"""Cost-reserving local relay for matched OpenAI Responses evaluations."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from benchmarks.v3_matched import BudgetLedger, _cost

MODEL = "gemini-3.5-flash-lite"
MAX_OUTPUT_TOKENS = 4096
MAX_REQUEST_BYTES = 20_000_000
MAX_ERROR_BODY_BYTES = 65_536


def _safe_error_code(error: object) -> str | None:
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else None


class ResponsesRelay:
    def __init__(self, *, endpoint: str, api_key: str, budget: BudgetLedger):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.client_key = secrets.token_urlsafe(24)
        self.budget = budget
        self.label = ("", "", -1)
        self.rows: list[dict] = []
        self.budget_blocked = False
        self.provider_error: str | None = None
        self._lock = threading.Lock()
        self._upstream_lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    @staticmethod
    def _usage(result: dict) -> dict | None:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        cache_write = (usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens", 0)))
        if isinstance(details, dict):
            cache_write = details.get("cache_write_tokens", details.get("cache_creation_tokens", cache_write))
        if not all(isinstance(value, int) and value >= 0
                   for value in (input_tokens, output_tokens, cached, cache_write)):
            return None
        if cached + cache_write > input_tokens:
            return None
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "cache_read_tokens": cached, "cache_write_tokens": cache_write}

    def _handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, status: int, data: dict) -> None:
                payload = json.dumps(data, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                self._forward_post()

            def _forward_post(self):
                if self.path.split("?", 1)[0] not in {"/v1/responses", "/responses"}:
                    self._json(404, {"error": {"message": "unsupported benchmark route"}})
                    return
                if self.headers.get("Authorization") != "Bearer " + relay.client_key:
                    self._json(401, {"error": {"message": "invalid benchmark relay credential"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BYTES:
                        self._json(413, {"error": {"message": "benchmark request exceeds its bound"}})
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("request must be an object")
                except (ValueError, json.JSONDecodeError):
                    self._json(400, {"error": {"message": "invalid benchmark request"}})
                    return

                # Panstar rejects system-role input items; normalize here so V2 and V3 share one wire shape.
                input_items = body.get("input")
                if isinstance(input_items, list):
                    system_messages = []
                    forwarded_items = []
                    for item in input_items:
                        if (isinstance(item, dict) and item.get("role") == "system"
                                and isinstance(item.get("content"), str)):
                            system_messages.append(item["content"])
                        else:
                            forwarded_items.append(item)
                    if system_messages:
                        instructions = body.get("instructions", "")
                        if not isinstance(instructions, str):
                            self._json(400, {"error": {"message": "invalid benchmark instructions"}})
                            return
                        body["instructions"] = "\n\n".join(
                            ([instructions] if instructions else []) + system_messages)
                        body["input"] = forwarded_items or "Continue."

                body["model"] = MODEL
                body["max_output_tokens"] = MAX_OUTPUT_TOKENS
                wants_stream = body.get("stream") is True
                body["stream"] = wants_stream
                encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                attempt_id = uuid.uuid4().hex
                input_bound = len(encoded) + 512
                arm, fixture_id, repeat = relay.label
                row = {"attempt_id": attempt_id, "arm": arm, "fixture_id": fixture_id,
                       "repeat": repeat, "request_sha256": hashlib.sha256(encoded).hexdigest(),
                       "model": MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
                       "reserved_rmb": str(_cost(input_bound, MAX_OUTPUT_TOKENS)),
                       "status": "unknown", "usage": None}
                started = time.monotonic()
                stream_started = False
                try:
                    with relay._upstream_lock:
                        if relay.provider_error:
                            self._json(503, {"error": {"message": "benchmark provider is disabled after an upstream error"}})
                            return
                        if not relay.budget.reserve(attempt_id, input_token_bound=input_bound,
                                                    output_token_bound=MAX_OUTPUT_TOKENS, metadata=row):
                            relay.budget_blocked = True
                            self._json(402, {"error": {"message": "benchmark spend cap reached"}})
                            return
                        with relay._lock:
                            relay.rows.append(row)
                        try:
                            upstream = requests.post(
                                relay.endpoint + "/responses", data=encoded,
                                headers={"Authorization": "Bearer " + relay.api_key,
                                         "Content-Type": "application/json"},
                                timeout=180, allow_redirects=False, stream=wants_stream,
                            )
                        except requests.RequestException as exc:
                            relay.provider_error = f"upstream_transport_{type(exc).__name__}"
                            relay.budget.settle(attempt_id, None)
                            row["transport_error"] = type(exc).__name__
                            self._json(502, {"error": {"message": "benchmark upstream transport failed"}})
                            return
                        row["http_status"] = upstream.status_code
                        if upstream.status_code != 200:
                            relay.budget.settle(attempt_id, None)
                            row["status"] = "unknown"
                            error_body = bytearray()
                            try:
                                for chunk in upstream.iter_content(chunk_size=4096):
                                    if not chunk:
                                        continue
                                    error_body.extend(chunk[:MAX_ERROR_BODY_BYTES - len(error_body) + 1])
                                    if len(error_body) > MAX_ERROR_BODY_BYTES:
                                        error_body.clear()
                                        break
                            except requests.RequestException:
                                error_body.clear()
                            try:
                                error_response = json.loads(error_body)
                            except (ValueError, json.JSONDecodeError):
                                error_response = None
                            error = error_response.get("error") if isinstance(error_response, dict) else None
                            code = _safe_error_code(error)
                            suffix = f":{code}" if code else ""
                            relay.provider_error = f"upstream_http_{upstream.status_code}{suffix}"
                            upstream.close()
                            self._json(upstream.status_code if 400 <= upstream.status_code < 600 else 502,
                                       {"error": {"message": f"benchmark upstream returned HTTP {upstream.status_code}"}})
                            return
                        if not wants_stream:
                            try:
                                response_body = upstream.json()
                            except ValueError:
                                response_body = None
                            if not isinstance(response_body, dict):
                                relay.provider_error = "upstream_invalid_json"
                                relay.budget.settle(attempt_id, None)
                                self._json(502, {"error": {"message": "benchmark upstream returned invalid JSON"}})
                                return
                    if not wants_stream:
                        usage = relay._usage(response_body)
                        relay.budget.settle(attempt_id, usage)
                        row["usage"] = usage
                        row["status"] = "settled" if usage is not None else "unknown"
                        row["actual_rmb"] = (str(_cost(usage["input_tokens"], usage["output_tokens"],
                                                         usage["cache_read_tokens"], usage["cache_write_tokens"]))
                                              if usage is not None else None)
                        self._json(200, response_body)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    stream_started = True
                    frame = bytearray()
                    terminal = False
                    for chunk in upstream.iter_content(chunk_size=4096):
                        if not chunk:
                            continue
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        frame.extend(chunk)
                        while True:
                            normalized = bytes(frame).replace(b"\r\n", b"\n")
                            if b"\n\n" not in normalized:
                                break
                            event, _, remainder = normalized.partition(b"\n\n")
                            frame = bytearray(remainder)
                            data = b"\n".join(line[5:].lstrip() for line in event.splitlines()
                                              if line.startswith(b"data:"))
                            if data and data != b"[DONE]":
                                try:
                                    message = json.loads(data)
                                except (ValueError, json.JSONDecodeError):
                                    continue
                                if message.get("type") in {
                                        "response.completed", "response.incomplete", "response.failed"}:
                                    if message["type"] == "response.failed":
                                        response = message.get("response")
                                        error = response.get("error") if isinstance(response, dict) else None
                                        code = _safe_error_code(error)
                                        suffix = f":{code}" if code else ""
                                        with relay._upstream_lock:
                                            relay.provider_error = f"upstream_response_failed{suffix}"
                                    usage = relay._usage(message.get("response", {}))
                                    relay.budget.settle(attempt_id, usage)
                                    row["usage"] = usage
                                    row["status"] = "settled" if usage is not None else "unknown"
                                    row["actual_rmb"] = (str(_cost(usage["input_tokens"], usage["output_tokens"],
                                                                     usage["cache_read_tokens"], usage["cache_write_tokens"]))
                                                          if usage is not None else None)
                                    upstream.close()
                                    terminal = True
                                    break
                        if terminal:
                            break
                except (requests.RequestException, OSError) as exc:
                    if isinstance(exc, requests.RequestException):
                        with relay._upstream_lock:
                            relay.provider_error = f"upstream_transport_{type(exc).__name__}"
                    if row["status"] == "unknown":
                        relay.budget.settle(attempt_id, None)
                        row["transport_error"] = type(exc).__name__
                    if not stream_started:
                        try:
                            self._json(502, {"error": {"message": "benchmark upstream transport failed"}})
                        except (OSError, BrokenPipeError):
                            pass
                finally:
                    row["latency_ms"] = round((time.monotonic() - started) * 1000, 2)

        return Handler
