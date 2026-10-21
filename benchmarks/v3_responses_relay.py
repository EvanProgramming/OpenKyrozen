"""Cost-reserving local relay for matched OpenAI Responses evaluations."""
from __future__ import annotations

import hashlib
import json
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


class ResponsesRelay:
    def __init__(self, *, endpoint: str, api_key: str, budget: BudgetLedger):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.client_key = secrets.token_urlsafe(24)
        self.budget = budget
        self.label = ("", "", -1)
        self.rows: list[dict] = []
        self._lock = threading.Lock()
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
        if not all(isinstance(value, int) and value >= 0 for value in (input_tokens, output_tokens, cached)):
            return None
        if cached > input_tokens:
            return None
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "cache_read_tokens": cached}

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

                body["model"] = MODEL
                body["max_output_tokens"] = MAX_OUTPUT_TOKENS
                body["stream"] = False
                encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                attempt_id = uuid.uuid4().hex
                input_bound = len(encoded) + 512
                if not relay.budget.reserve(attempt_id, input_token_bound=input_bound,
                                            output_token_bound=MAX_OUTPUT_TOKENS):
                    self._json(402, {"error": {"message": "benchmark spend cap reached"}})
                    return
                arm, fixture_id, repeat = relay.label
                row = {"attempt_id": attempt_id, "arm": arm, "fixture_id": fixture_id,
                       "repeat": repeat, "request_sha256": hashlib.sha256(encoded).hexdigest(),
                       "model": MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
                       "reserved_usd": str(_cost(input_bound, MAX_OUTPUT_TOKENS)),
                       "status": "unknown", "usage": None}
                with relay._lock:
                    relay.rows.append(row)

                started = time.monotonic()
                try:
                    upstream = requests.post(
                        relay.endpoint + "/responses", data=encoded,
                        headers={"Authorization": "Bearer " + relay.api_key,
                                 "Content-Type": "application/json"},
                        timeout=180, allow_redirects=False,
                    )
                    try:
                        response_body = upstream.json()
                    except ValueError:
                        response_body = None
                    row["http_status"] = upstream.status_code
                    if upstream.status_code == 200 and isinstance(response_body, dict):
                        usage = relay._usage(response_body)
                        relay.budget.settle(attempt_id, usage)
                        row["usage"] = usage
                        row["status"] = "settled" if usage is not None else "unknown"
                        row["actual_usd"] = (str(_cost(usage["input_tokens"], usage["output_tokens"],
                                                         usage["cache_read_tokens"]))
                                              if usage is not None else None)
                        self._json(200, response_body)
                    else:
                        relay.budget.settle(attempt_id, None)
                        row["status"] = "unknown"
                        self._json(upstream.status_code if 400 <= upstream.status_code < 600 else 502,
                                   {"error": {"message": f"benchmark upstream returned HTTP {upstream.status_code}"}})
                except requests.RequestException as exc:
                    relay.budget.settle(attempt_id, None)
                    row["status"] = "unknown"
                    row["transport_error"] = type(exc).__name__
                    self._json(502, {"error": {"message": "benchmark upstream transport failed"}})
                finally:
                    row["latency_ms"] = round((time.monotonic() - started) * 1000, 2)

        return Handler
