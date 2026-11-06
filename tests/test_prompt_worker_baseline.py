"""The candidate observer must run against the actual immutable V2 snapshot."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import BASELINE, _extract_baseline, _worker_env, prepare_fixture, verify_fixture


class FrozenBaselineWorkerTests(unittest.TestCase):
    def test_actual_frozen_v2_worker_imports_and_observes_eleven_local_transport_turns(self):
        candidate = Path(__file__).resolve().parents[1]
        case = next(item for item in FIXTURES if item["fixture_id"] == "long_context-1")
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                items = request["input"]
                last = items[-1].get("content", "")
                text = json.dumps(case["verifier"]["expected"]) if str(last).startswith("Return only a JSON") else "Acknowledged the current facts."
                if any("UNTRUSTED TRANSCRIPT:" in str(item.get("content", "")) for item in items):
                    text = "owner team-cedar; rollback release-17; latest approved region ap-southeast-1"
                response = {"id": "local-offline-response", "object": "response", "status": "completed",
                    "model": "offline-fixture", "output": [{"type": "message", "id": "message-local",
                    "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}]}
                if request.get("stream"):
                    events = [{"type": "response.output_text.delta", "delta": text},
                              {"type": "response.completed", "response": response}]
                    payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                else: payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            baseline, workspace, home = base / "baseline", base / "workspace", base / "home"
            _extract_baseline(baseline, candidate, BASELINE)
            prepared = prepare_fixture(workspace, case)
            home.mkdir()
            prompt, conversation, result = base / "prompt.txt", base / "conversation.json", base / "result.json"
            prompt.write_text(case["prompt"])
            conversation.write_text(json.dumps({"followups": case["turns"][1:], "context_window_tokens": 16384,
                                               "output_reserve_tokens": 4096}))
            env = _worker_env(base, case, type("LocalRelay", (), {"client_key": "offline-local"})())
            env["KYROZEN_APPROVAL_MODE"] = "never"
            completed = subprocess.run([sys.executable, str(candidate / "benchmarks" / "prompt_worker.py"),
                "--source", str(baseline), "--workspace", str(workspace), "--url", f"http://127.0.0.1:{server.server_port}/v1",
                "--model", "offline-fixture", "--protocol", "responses", "--prompt-file", str(prompt),
                "--conversation-file", str(conversation), "--result", str(result)],
                cwd=workspace, env=env, text=True, capture_output=True, timeout=90)
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            worker = json.loads(result.read_text())
            self.assertEqual(len(worker["conversation_records"]), 11)
            self.assertEqual(len(worker["request_context_records"]), len(requests))
            # This immutable V2 ask path loses prior turns. Observation must expose
            # its absent pressure/compaction independently of the physical task oracle.
            self.assertFalse(any(item["kind"] == "summary" for item in worker["request_context_records"]))
            self.assertEqual(len({item["component_estimates"]["conversation"] for item in worker["request_context_records"]}), 1)
            for index, request in enumerate(requests):
                visible_users = [item.get("content") for item in request["input"] if item.get("role") == "user"]
                self.assertFalse(any(old_prompt in visible_users for old_prompt in case["turns"][:index]))
            self.assertTrue(all(item["retained_history_tokens"] is None for item in worker["request_context_records"]))
            self.assertTrue(all(item["provider_input_tokens"] is None for item in worker["request_context_records"]))
            verified = verify_fixture(case, workspace, worker["answer"], worker, prepared)
            self.assertEqual(verified["result"], "passed")  # The fake transport supplies correct facts.
            self.assertIs(verified["context_compaction_observed"], False)
            # A real V2 receipt is a plain-string projection, not V3 typed arguments.
            receipt_script = '''import json,runpy,sys
sys.path.insert(0,sys.argv[1])
from openkyrozen.app.bootstrap import build_application
helpers=runpy.run_path(sys.argv[2])
app=build_application(surface='cli')
try:
 app.runtime._set_workspace_root(sys.argv[3])
 receipt=app.runtime._execute_turn_action('read_file','README.md',operation_scope='offline-observation',successful_operations=set())
 print('OBSERVED='+json.dumps(helpers['_normalize_tool_receipt'](receipt.as_dict(),sys.argv[3])))
finally: app.close()
'''
            receipt_result = subprocess.run([sys.executable, "-c", receipt_script, str(baseline),
                str(candidate / "benchmarks" / "prompt_worker.py"), str(workspace)], cwd=workspace, env=env,
                text=True, capture_output=True, timeout=30)
            self.assertEqual(receipt_result.returncode, 0, receipt_result.stderr[-2000:])
            observed = json.loads(next(line[9:] for line in receipt_result.stdout.splitlines() if line.startswith("OBSERVED=")))
            self.assertEqual(observed["target_path"], "README.md")
            self.assertTrue(observed["success"])
            self.assertTrue(observed["authorized"])
            self.assertIsNone(observed["background"])


if __name__ == "__main__": unittest.main()
