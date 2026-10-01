import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from benchmarks.prompt_comparison import ARMS, SETTINGS, Budget, Relay, codewhale_answer, create_fixture, summarize, usage_total
from http.server import ThreadingHTTPServer
import threading


class PromptComparisonTests(unittest.TestCase):
    def test_reservations_usage_and_failed_calls(self):
        budget = Budget(2, 100)
        self.assertTrue(budget.reserve(60))
        self.assertFalse(budget.reserve(60))
        budget.settle(60, 10)
        self.assertTrue(budget.reserve(60))
        budget.settle(60, None)
        self.assertEqual(budget.tokens, 70)
        self.assertFalse(budget.reserve(1))
        self.assertIsNone(usage_total(None))
        self.assertIsNone(usage_total({"prompt_tokens": 1}))
        self.assertEqual(usage_total({"prompt_tokens": 10, "completion_tokens": 20,
                                    "completion_tokens_details": {"reasoning_tokens": 15}}), 30)

    def test_claim_requires_complete_passing_pairs_and_known_usage(self):
        rows = [{"arm": arm, "case": "one", "repeat": 0, "passed": True, "elapsed_ms": 10} for arm in ARMS]
        calls = [{"arm": arm, "usage": {"prompt_tokens": 40 if arm == "compact" else 100, "completion_tokens": 10},
                  "status": "ok", "request_settings": SETTINGS} for arm in ARMS]
        result = summarize(rows, calls, 1)
        self.assertTrue(result["comparisons"]["baseline"]["pilot_claim_supported"])
        self.assertFalse(result["general_superiority_supported"])
        rows[1]["passed"] = False
        self.assertFalse(summarize(rows, calls, 1)["comparisons"]["baseline"]["pilot_claim_supported"])
        rows[1]["passed"] = True
        calls[1]["usage"] = None
        self.assertFalse(summarize(rows, calls, 1)["comparisons"]["baseline"]["pilot_claim_supported"])
        calls[1]["usage"] = {"prompt_tokens": 40, "completion_tokens": 10}
        self.assertFalse(summarize(rows, calls, 2)["comparisons"]["baseline"]["pilot_claim_supported"])

    def test_relay_pins_settings_exports_usage_and_never_credentials(self):
        relay = Relay("private-test-key", "https://example.invalid/v1", Budget(1, 100000))
        relay.label = ("compact", "one", 0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), relay.handler())
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"id": "test", "model": SETTINGS["model"], "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
        real_post = requests.post
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        with patch("benchmarks.prompt_comparison.requests.post", return_value=response) as upstream:
            result = real_post(url, json={"model": SETTINGS["model"], "messages": [{"role": "user", "content": "private"}], "stream": True})
            self.assertIn("[DONE]", result.text)
            payload = json.loads(upstream.call_args.kwargs["data"])
            self.assertEqual({k: payload[k] for k in SETTINGS}, SETTINGS)
            self.assertEqual(relay.budget.tokens, 12)
            self.assertEqual(real_post(url, json=payload).status_code, 402)
            self.assertEqual(upstream.call_count, 1)
        exported = json.dumps(relay.rows)
        self.assertNotIn("private-test-key", exported)
        self.assertNotIn('"content"', exported)
        self.assertEqual(relay.rows[0]["status"], "ok")

    def test_codewhale_grades_answer_content_without_tool_results(self):
        transcript = '\n'.join(json.dumps(event) for event in [
            {"type": "tool_result", "output": "correct answer"},
            {"type": "content", "content": "final answer"},
            {"type": "session_capture", "content": "private transcript"},
            {"type": "metadata", "meta": {"status": "completed"}},
            {"type": "done"},
        ])
        self.assertEqual(codewhale_answer(transcript), "final answer")
        self.assertEqual(codewhale_answer(json.dumps({"type": "tool_result", "output": "answer"})), "")

    def test_fixture_git_history_is_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            first, first_hash = create_fixture(Path(temp) / "one")
            second, second_hash = create_fixture(Path(temp) / "two")
            self.assertEqual(first, second)
            self.assertEqual(first_hash, second_hash)
