"""Bounded live DeepSeek comparison: baseline, compact OpenKyrozen, CodeWhale.

All state and model-produced code lives in a disposable temporary directory.
Only sanitized metrics and fixed-fixture results are exported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
MODEL = "deepseek-v4-flash"
SETTINGS = {"model": MODEL, "thinking": {"type": "disabled"}, "temperature": 0,
            "top_p": 1, "max_tokens": 4096, "stream": False}
ARMS = ("baseline", "compact", "codewhale")
CASES = {
    "investigate": "Inspect catalog.py and README.md. Answer which function sorts the product names, and the exact sorting rule. Do not change files. Keep the final answer short.",
    "fix": "Fix mean([]) in stats.py to return 0. Preserve behavior for nonempty lists. Inspect the code, reproduce the failure, apply the minimal fix and run python3 -m unittest test_stats -v. Report the result.",
    "implement": "Implement unique_sorted(values) in catalog.py: return distinct strings sorted case-insensitively, with case-sensitive lexical order breaking ties. Preserve all other functions. Run python3 -m unittest test_catalog -v and report the result.",
    "history": "Inspect the Git commit history using the dedicated Git history tools, not shell commands. Identify the commit subject that changed LIMIT from 10 to 20, its short commit hash and the old/new values. Do not change files. Keep the final answer short.",
}


def create_fixture(root):
    root.mkdir(parents=True)
    files = {
        "README.md": "Product names are sorted by sorted_products in catalog.py, by casefold then the original spelling.\n",
        "catalog.py": "def sorted_products(names):\n    return sorted(names, key=lambda name: (name.casefold(), name))\n\ndef unique_sorted(values):\n    raise NotImplementedError\n\nLIMIT = 10\n",
        "stats.py": "def mean(values):\n    return sum(values) / len(values)\n",
        "test_stats.py": "import unittest\nfrom stats import mean\nclass StatsTest(unittest.TestCase):\n def test_empty(self): self.assertEqual(mean([]), 0)\n def test_values(self): self.assertEqual(mean([2,4,6]), 4)\n",
        "test_catalog.py": "import unittest\nfrom catalog import unique_sorted, sorted_products\nclass CatalogTest(unittest.TestCase):\n def test_empty(self): self.assertEqual(unique_sorted([]), [])\n def test_ties(self): self.assertEqual(unique_sorted(['b','a','A','b']), ['A','a','b'])\n def test_existing(self): self.assertEqual(sorted_products(['z','A','a']), ['A','a','z'])\n",
    }
    for name, content in files.items():
        (root / name).write_text(content)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE="2026-10-01T00:00:00Z", GIT_COMMITTER_DATE="2026-10-01T00:00:00Z")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], env=env, stderr=subprocess.DEVNULL, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Benchmark")
    git("config", "user.email", "benchmark@example.invalid")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "initial fixture")
    (root / "catalog.py").write_text(files["catalog.py"].replace("LIMIT = 10", "LIMIT = 20"))
    git("add", "catalog.py")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "raise catalog limit")
    return files, git("rev-parse", "--short", "HEAD")


def codewhale_answer(transcript):
    """Only grade the terminal result, never prompts or tool-result content."""
    for line in reversed(transcript.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "content" and isinstance(event.get("content"), str):
            return event["content"]
        if event.get("type") == "result":
            for field in ("result", "text", "content", "summary"):
                if isinstance(event.get(field), str):
                    return event[field]
        if event.get("type") == "assistant":
            message = event.get("message", {})
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text = "".join(item.get("text", "") for item in content if item.get("type") == "text")
                if text:
                    return text
    return ""


def usage_total(usage):
    # completion_tokens already includes reasoning on this API.
    if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
        return None
    return int(usage["prompt_tokens"]) + int(usage["completion_tokens"])


class Budget:
    def __init__(self, requests_limit, tokens_limit):
        self.requests_limit, self.tokens_limit = requests_limit, tokens_limit
        self.requests = self.tokens = 0
        self.lock = threading.Lock()

    def reserve(self, bound):
        with self.lock:
            if self.requests >= self.requests_limit or self.tokens + bound > self.tokens_limit:
                return False
            self.requests += 1
            self.tokens += bound
            return True

    def settle(self, bound, actual):
        with self.lock:
            if actual is not None:
                self.tokens += actual - bound


class Relay:
    def __init__(self, key, endpoint, budget):
        self.key, self.endpoint, self.budget = key, endpoint.rstrip("/"), budget
        self.rows = []
        self.label = None
        self.tool_names = []

    def handler(self):
        relay = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path.split("?")[0] not in {"/chat/completions", "/v1/chat/completions"}:
                    self.send_error(404)
                    return
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    if data.get("model") != MODEL:
                        self.send_error(400, "benchmark model mismatch")
                        return
                    stream = bool(data.get("stream"))
                    original = {k: data.get(k) for k in SETTINGS}
                    for k in ("reasoning_effort", "stream_options", "presence_penalty", "frequency_penalty"):
                        data.pop(k, None)
                    data.update(SETTINGS)
                    body = json.dumps(data).encode()
                    # UTF-8 bytes + framing margin conservatively bound input;
                    # reserve the entire maximum output before forwarding.
                    bound = len(body) + 1024 + SETTINGS["max_tokens"]
                    if not relay.budget.reserve(bound):
                        self.send_error(402, "benchmark budget exhausted")
                        return
                    row = {"arm": relay.label[0], "case": relay.label[1], "repeat": relay.label[2],
                           "request_settings": dict(SETTINGS), "original_settings": original,
                           "request_hash": hashlib.sha256(body).hexdigest(), "reserved_tokens": bound,
                           "usage": None, "status": "error"}
                    relay.rows.append(row)
                    row["tool_action_names"] = []
                    started = time.monotonic()
                    try:
                        response = requests.post(relay.endpoint + "/chat/completions", data=body,
                                                 headers={"Authorization": "Bearer " + relay.key,
                                                          "Content-Type": "application/json"}, timeout=180)
                        row["http_status"] = response.status_code
                        response.raise_for_status()
                        result = response.json()
                        row["usage"] = result.get("usage")
                        row["effective_model"] = result.get("model")
                        row["tool_calls"] = sum(len(c.get("message", {}).get("tool_calls") or []) for c in result.get("choices", []))
                        import re
                        for choice in result.get("choices", []):
                            message = choice.get("message", {})
                            row["tool_action_names"].extend(re.findall(r'"action"\s*:\s*"([a-zA-Z0-9_]+)"', message.get("content") or ""))
                            row["tool_action_names"].extend(call.get("function", {}).get("name", "") for call in message.get("tool_calls") or [])
                        row["status"] = "ok" if usage_total(row["usage"]) is not None else "missing_usage"
                        relay.budget.settle(bound, usage_total(row["usage"]))
                        payload = json.dumps(result).encode()
                        if stream:
                            chunks = []
                            for choice in result.get("choices", []):
                                message = dict(choice.get("message", {}))
                                message.pop("reasoning_content", None)
                                for index, call in enumerate(message.get("tool_calls") or []):
                                    call["index"] = index
                                chunk = {"id": result["id"], "object": "chat.completion.chunk", "model": result["model"],
                                         "created": result["created"], "choices": [{"index": choice["index"], "delta": message,
                                         "finish_reason": choice.get("finish_reason")}], "usage": result.get("usage")}
                                chunks.append("data: " + json.dumps(chunk) + "\n\n")
                            payload = ("".join(chunks) + "data: [DONE]\n\n").encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    except Exception as exc:
                        row["error_type"] = type(exc).__name__
                        self.send_error(502, "upstream request failed")
                    finally:
                        row["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                except (ValueError, KeyError, TypeError):
                    self.send_error(400, "invalid benchmark request")
        return Handler


def credential():
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("KYROZEN_API_KEY")
    if key:
        return key
    path = Path.home() / ".kyrozen_config.json"
    data = json.loads(path.read_text())
    key = data.get("api_key", "")
    if data.get("encrypted") and key.startswith("v2:"):
        from cryptography.fernet import Fernet
        key = Fernet((Path.home() / ".kyrozen_secret").read_bytes().strip()).decrypt(key[3:].encode()).decode()
    elif data.get("encrypted"):
        sys.path.insert(0, str(ROOT))
        from openkyrozen.security.credentials import decrypt_api_key
        key = decrypt_api_key(key)
    if not key or data.get("provider", "deepseek") != "deepseek":
        raise ValueError("no usable DeepSeek credential")
    return key


def summarize(rows, calls, expected_runs):
    groups = {}
    for arm in ARMS:
        turns = [r for r in rows if r["arm"] == arm]
        requests_ = [r for r in calls if r["arm"] == arm]
        known = [usage_total(r["usage"]) for r in requests_]
        groups[arm] = {"runs": len(turns), "passed": sum(r["passed"] for r in turns),
                       "requests": len(requests_), "total_tokens": sum(t or 0 for t in known),
                       "input_tokens": sum((r["usage"] or {}).get("prompt_tokens", 0) for r in requests_),
                       "output_tokens": sum((r["usage"] or {}).get("completion_tokens", 0) for r in requests_),
                       "usage_complete": all(t is not None for t in known),
                       "median_ms": round(statistics.median(r["elapsed_ms"] for r in turns), 2) if turns else None}
    comparisons = {}
    for other in ("baseline", "codewhale"):
        left = {(r["case"], r["repeat"]): r for r in rows if r["arm"] == "compact"}
        right = {(r["case"], r["repeat"]): r for r in rows if r["arm"] == other}
        complete = len(left) == len(right) == expected_runs and left.keys() == right.keys()
        quality = complete and all(left[k]["passed"] and right[k]["passed"] for k in left)
        usage_known = groups[other]["usage_complete"] and groups["compact"]["usage_complete"]
        savings = 1 - groups["compact"]["total_tokens"] / groups[other]["total_tokens"] if groups[other]["total_tokens"] else None
        settings_valid = all(r["status"] == "ok" and r["request_settings"] == SETTINGS for r in calls
                             if r["arm"] in {other, "compact"})
        if not complete or not usage_known:
            savings = None
        comparisons[other] = {"complete": complete, "same_passing_outcomes": quality,
                              "token_savings_fraction": round(savings, 4) if savings is not None else None,
                              "pilot_claim_supported": bool(quality and usage_known and settings_valid and savings is not None and savings >= .10)}
    return {"summary": groups, "comparisons": comparisons, "general_superiority_supported": False}


def run(args):
    key = credential()
    budget = Budget(args.max_requests, args.max_tokens)
    relay = Relay(key, args.endpoint, budget)
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    rows = []
    with tempfile.TemporaryDirectory(prefix="kyrozen-prompt-benchmark-") as temp:
        try:
            for repeat in range(args.repeats):
                for case_index, (case, prompt) in enumerate(CASES.items()):
                    offset = (repeat + case_index) % len(ARMS)
                    for arm in ARMS[offset:] + ARMS[:offset]:
                        if budget.requests >= args.max_requests:
                            break
                        base = Path(temp) / f"{repeat}-{case}-{arm}"
                        workspace, home = base / "workspace", base / "home"
                        home.mkdir(parents=True)
                        files, expected_hash = create_fixture(workspace)
                        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.glob("*.py")}
                        prompt_file, result_file = base / "prompt.txt", base / "result.json"
                        prompt_file.write_text(prompt)
                        relay.label = (arm, case, repeat)
                        env = {k: v for k, v in os.environ.items() if not any(word in k for word in ("API_KEY", "TOKEN", "KYROZEN_", "CODEWHALE_"))}
                        env.update(HOME=str(home), DEEPSEEK_API_KEY="benchmark-local", CODEWHALE_TELEMETRY="0",
                                   KYROZEN_DB_PATH=str(base / "state.sqlite3"), KYROZEN_DISABLE_VECTOR_INDEX="1",
                                   KYROZEN_APPROVAL_MODE="never", KYROZEN_PROMPT_PROFILE="compact" if arm == "compact" else "classic",
                                   KYROZEN_PROVIDER_TIMEOUT_SECONDS="200", GIT_CONFIG_GLOBAL=os.devnull,
                                   PYTHONPATH="", KYROZEN_PROVIDER="deepseek", KYROZEN_BASE_URL=url,
                                   KYROZEN_MODEL_SIMPLE=MODEL, KYROZEN_MODEL_COMPLEX=MODEL)
                        if arm == "codewhale":
                            command = [args.codewhale, "--provider", "deepseek", "--model", MODEL, "--base-url", url,
                                       "--telemetry", "false", "--no-project-config", "--fresh", "-C", str(workspace),
                                       "exec", "--auto", "--max-turns", "12", "--output-format", "stream-json", prompt]
                        else:
                            source = args.baseline if arm == "baseline" else args.source
                            command = [sys.executable, str(ROOT / "benchmarks/prompt_worker.py"), "--source", str(source),
                                       "--workspace", str(workspace), "--url", url, "--prompt-file", str(prompt_file), "--result", str(result_file)]
                        started = time.monotonic()
                        error = None
                        try:
                            process = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, timeout=600)
                            code = process.returncode
                            result = json.loads(result_file.read_text()) if result_file.exists() else {}
                            output = result.get("answer", codewhale_answer(process.stdout) if arm == "codewhale" else "")
                            if arm == "codewhale":
                                result["tool_calls"] = sum(json.loads(line).get("type") == "tool_use" for line in process.stdout.splitlines() if line.startswith("{"))
                            (base / "stdout.txt").write_text(process.stdout)
                            (base / "stderr.txt").write_text(process.stderr)
                        except subprocess.TimeoutExpired:
                            code, output, result, error = -1, "", {}, "timeout"
                        # Check model code in another subprocess, with no real credentials.
                        checks = None
                        if case in {"fix", "implement"}:
                            suite = "test_stats" if case == "fix" else "test_catalog"
                            try:
                                check_code = files[suite + ".py"] + "\nunittest.main()\n"
                                checks = subprocess.run([sys.executable, "-c", check_code], cwd=workspace,
                                                        env=env, capture_output=True, timeout=20).returncode == 0
                            except subprocess.TimeoutExpired:
                                checks = False
                        unchanged = all(hashlib.sha256((workspace / name).read_bytes()).hexdigest() == digest
                                        for name, digest in before.items() if (workspace / name).exists()) and all((workspace / name).exists() for name in before)
                        if case == "investigate":
                            checks = unchanged and "sorted_products" in output and "casefold" in output.lower() and any(
                                term in output.lower() for term in ("original", "tie", "lexical", "case-sensitive", "spelling", "name.casefold(), name"))
                        elif case == "history":
                            checks = unchanged and "raise catalog limit" in output.lower() and expected_hash in output and "10" in output and "20" in output
                        calls = [c for c in relay.rows if (c["arm"], c["case"], c["repeat"]) == relay.label]
                        if case in {"fix", "implement"}:
                            target = "stats.py" if case == "fix" else "catalog.py"
                            checks = checks and all((workspace / name).exists() and hashlib.sha256((workspace / name).read_bytes()).hexdigest() == digest
                                                    for name, digest in before.items() if name != target)
                        task_complete = result.get("tasks") is None or all(status == "succeeded" for status in result.get("tasks", []))
                        passed = bool(code == 0 and checks and task_complete and calls and all(c["status"] == "ok" for c in calls))
                        row = {"arm": arm, "case": case, "repeat": repeat, "passed": passed, "acceptance_passed": bool(checks),
                               "exit_code": code, "error": error, "task_lifecycle_complete": task_complete, "answer_hash": hashlib.sha256(output.encode()).hexdigest(),
                               "tool_calls": result.get("tool_calls", sum(c.get("tool_calls", 0) for c in calls)),
                               "task_statuses": result.get("tasks"), "requests": len(calls),
                               "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
                        rows.append(row)
                        Path(args.output).write_text(json.dumps({"protocol": "openkyrozen-prompt-live-v1", "in_progress": True,
                                                               "rows": rows, "calls": relay.rows}, indent=2) + "\n")
                        print(json.dumps({k: row[k] for k in ("arm", "case", "repeat", "passed", "requests")}), flush=True)
                        # Local transcripts are disposable and never exported. Retain a short
                        # non-secret diagnostic only on this terminal if process setup fails.
                        if not calls and code:
                            print("worker setup failed:", (process.stderr if 'process' in locals() else error)[-1200:], file=sys.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    report = {"protocol": "openkyrozen-prompt-live-v1", "model": MODEL, "settings": SETTINGS,
              "limits": {"requests": args.max_requests, "tokens": args.max_tokens}, "budget_tokens_charged": budget.tokens,
              "codewhale_version": subprocess.check_output([args.codewhale, "--version"], text=True).strip(),
              "fixture_prompts": CASES, "rows": rows, "calls": relay.rows,
              **summarize(rows, relay.rows, len(CASES) * args.repeats)}
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["comparisons"], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--codewhale", default=str(Path.home() / ".local/bin/codewhale"))
    parser.add_argument("--endpoint", default="https://api.deepseek.com/v1")
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 100 or not 1 <= args.max_tokens <= 1_000_000 or not 1 <= args.repeats <= 2:
        parser.error("pilot limits: 1-100 requests, 1-1000000 tokens, 1-2 repeats")
    run(args)


if __name__ == "__main__":
    main()
