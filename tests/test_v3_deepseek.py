"""Offline coverage of genuine CNY pricing, historical exposure, and relay bounds."""
import dataclasses
import json
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from benchmarks import v3_matched


class DeepSeekStrictChatTests(unittest.TestCase):
    def test_strict_schema_preserves_optional_native_values(self):
        from benchmarks.v3_deepseek import strict_chat_body, strict_chat_tools
        schema = {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1},
        }, "required": ["path"], "additionalProperties": False}
        function = {"name": "read_file", "parameters": schema}
        result = strict_chat_body({"model": "deepseek-flash", "stream": True,
            "messages": [{"role": "user", "content": "Read the file."}],
            "tools": strict_chat_tools([{"description": "Read", **function}])})
        self.assertEqual(result["thinking"], {"type": "enabled"})
        self.assertEqual(result["stream_options"], {"include_usage": True})
        wire = result["tools"][0]["function"]
        self.assertTrue(wire["strict"])
        variants = wire["parameters"]["properties"]["arguments"]["anyOf"]
        self.assertEqual([schema["required"] for schema in variants], [["path"], ["path", "start_line"]])
        self.assertEqual(variants[1]["properties"], {"path": {"type": "string"},
                                                    "start_line": {"type": "integer", "minimum": 1}})
        self.assertNotIn("strict", function)
        self.assertIn("minLength", schema["properties"]["path"])

    def test_complete_catalogue_fits_wire_and_typed_plan_maps_round_trip(self):
        from benchmarks.v3_deepseek import decode_strict_chat_arguments, strict_chat_body, strict_chat_schema, strict_chat_tools
        from openkyrozen.tools.catalog import _SCHEMAS
        from openkyrozen.tools.manifest import ToolRegistry
        wire = strict_chat_body({"model": "deepseek-flash", "messages": [{"role": "user", "content": "plan"}],
            "tools": strict_chat_tools([
                {"name": name, "description": "", "parameters": schema}
                for name, schema in json.loads(json.dumps(_SCHEMAS)).items()])})
        self.assertLess(len(json.dumps(wire).encode()), 100_000)

        def assert_provider_contract(node):
            if isinstance(node, list):
                for value in node:
                    assert_provider_contract(value)
            elif isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertTrue(node.get("properties"))
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                if "anyOf" in node:
                    self.assertNotIn("properties", node)
                    self.assertTrue(all("type" in branch for branch in node["anyOf"]))
                for value in node.values():
                    assert_provider_contract(value)

        for tool in wire["tools"]:
            assert_provider_contract(tool["function"]["parameters"])
        self.assertEqual(json.loads(decode_strict_chat_arguments(
            '{"arguments":{"is_empty":true}}', _SCHEMAS["git_diff"])), {})
        with self.assertRaises(ValueError):
            strict_chat_schema({"anyOf": [{"$ref": "#/$def/unknown"}]})
        result = {"complexity": "simple", "tasks": [{"description": "Read value",
            "acceptance": [{"criterion": "Check value", "verification": {"action": "read_file",
                "arguments": {"key_value_entries": [{"key": "path", "value": "proof.txt"}]},
                "expect": {"equals": "result"}}}]}]}
        schema = _SCHEMAS["update_plan"]
        decoded = json.loads(decode_strict_chat_arguments(json.dumps({"arguments": result}), schema))
        self.assertIsNone(ToolRegistry._schema_error(schema, decoded))
        verification = decoded["tasks"][0]["acceptance"][0]["verification"]
        self.assertEqual(verification["arguments"], {"path": "proof.txt"})
        # Reproduce the accepted beta response that used canonical arguments in the referenced map.
        captured = {"arguments": {"complexity": "simple", "expected_revision": 0,
            "reason": "Verify the planning preflight", "operations": [
                {"op": "add", "ref": "proof", "task": {"description": "Read and verify proof.txt",
                    "acceptance": [{"criterion": "The complete proof file has the expected marker",
                        "verification": {"action": "read_file", "arguments": {"path": "proof.txt"},
                                         "expect": {"equals": "planning-preflight-ok\n"}}}]}},
                {"op": "start", "task": "proof"}]}}
        self.assertEqual(json.loads(decode_strict_chat_arguments(json.dumps(captured), schema)), captured["arguments"])
        read_schema = _SCHEMAS["read_file"]
        self.assertEqual(json.loads(decode_strict_chat_arguments('{"path":"proof.txt"}', read_schema)),
                         {"path": "proof.txt"})
        invalid_read = json.loads(decode_strict_chat_arguments('{"path":false}', read_schema))
        self.assertIsNotNone(ToolRegistry._argument_error(read_schema, invalid_read))
        env = {"command": "pwd", "env": {"key_value_entries": "example"}}
        self.assertEqual(json.loads(decode_strict_chat_arguments(json.dumps(env), _SCHEMAS["run_cmd"])), env)
        arguments = {"arguments": ["--help"]}
        self.assertEqual(json.loads(decode_strict_chat_arguments(json.dumps(arguments), _SCHEMAS["github_read"])),
                         arguments)
        entries = result["tasks"][0]["acceptance"][0]["verification"]["arguments"]["key_value_entries"]
        entries.append({"key": "path", "value": "duplicate.txt"})
        with self.assertRaises(ValueError):
            decode_strict_chat_arguments(json.dumps({"arguments": result}), schema)
        with self.assertRaises(ValueError):
            decode_strict_chat_arguments('{"complexity":"simple","complexity":"complex"}', schema)

    def test_valid_json_reaches_canonical_validation_and_respects_object_cardinality(self):
        from benchmarks.v3_deepseek import decode_strict_chat_arguments, strict_chat_schema
        from openkyrozen.tools.catalog import _SCHEMAS
        from openkyrozen.tools.manifest import ToolRegistry
        expect = {"equals": {"type": "string"}, "contains": {"type": "string"}}
        predicate = {"type": "object", "properties": expect, "required": [],
                     "minProperties": 1, "maxProperties": 1, "additionalProperties": False}
        variants = strict_chat_schema(predicate)["properties"]["arguments"]["anyOf"]
        self.assertEqual([item["required"] for item in variants], [["equals"], ["contains"]])

        invalid = {"complexity": "simple", "tasks": [{"description": "Read source",
            "acceptance": [{"criterion": "read it", "verification": {"action": "read_file",
                "arguments": {"path": "source.py"}, "expect": {"equals": "a", "contains": "b"}}}]}]}
        plan_schema = _SCHEMAS["update_plan"]
        for raw in (invalid, {"arguments": invalid}):
            decoded = json.loads(decode_strict_chat_arguments(json.dumps(raw), plan_schema))
            self.assertEqual(decoded, invalid)
            self.assertIsNotNone(ToolRegistry._argument_error(plan_schema, decoded))

    def test_strict_usage_requires_the_chat_contract_and_consistent_cache_counts(self):
        from benchmarks.v3_deepseek import deepseek_usage
        raw = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
               "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 20,
               "completion_tokens_details": {"reasoning_tokens": 7}}
        response = {"usage": raw}
        self.assertIsNone(deepseek_usage(response))
        self.assertEqual(deepseek_usage(response, strict_chat=True), {
            "input_tokens": 80, "output_tokens": 20, "cache_read_tokens": 60,
            "cache_write_tokens": 0, "reasoning_tokens": 7})
        for bad in ({"prompt_cache_miss_tokens": 80}, {"total_tokens": 99},
                    {"prompt_tokens_details": {"cached_tokens": 59}}):
            self.assertIsNone(deepseek_usage({"usage": {**raw, **bad}}, strict_chat=True))

    def test_smoke_replays_reasoning_and_correlated_tool_reply(self):
        from benchmarks.v3_deepseek import (DEEPSEEK_ENDPOINT, DEEPSEEK_PRICING,
                                            DEEPSEEK_STRICT_RELAY_CONFIG, run_smoke)
        from benchmarks.v3_responses_relay import ResponsesRelay
        from unittest.mock import Mock, patch

        bodies = []

        def upstream(url, *, data, **_options):
            self.assertEqual(url, DEEPSEEK_ENDPOINT + "/beta/chat/completions")
            body = json.loads(data)
            bodies.append(body)
            function = body["tools"][0]["function"]
            self.assertTrue(function["strict"])
            value, = function["parameters"]["properties"]["value"]["enum"]
            call = {"id": "call-1", "type": "function", "function": {"name": "echo_probe",
                    "arguments": json.dumps({"value": value})}}
            if len(bodies) == 1:
                message = {"role": "assistant", "content": None, "reasoning_content": "use one probe",
                           "tool_calls": [call]}
                finish = "tool_calls"
            else:
                assistant, reply, system = body["messages"][1:]
                self.assertEqual(assistant["reasoning_content"], "use one probe")
                self.assertEqual(assistant["tool_calls"], [call])
                self.assertEqual(reply["tool_call_id"], call["id"])
                self.assertEqual(json.loads(reply["content"]), {"is_error": False, "result": {"value": value}})
                self.assertEqual(system["role"], "system")
                message, finish = {"role": "assistant", "content": value}, "stop"
            return Mock(status_code=200, **{"json.return_value": {
                "id": f"answer-{len(bodies)}", "object": "chat.completion", "created": 1,
                "model": "DeepSeek-V4.1-Flash-1017",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                          "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 3},
            }})

        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / "budget.sqlite", limit_rmb="15",
                                            pricing=DEEPSEEK_PRICING)
            relay = ResponsesRelay(endpoint=DEEPSEEK_ENDPOINT, api_key="offline", budget=ledger,
                                   config=DEEPSEEK_STRICT_RELAY_CONFIG)
            relay.start()
            try:
                with patch("benchmarks.v3_responses_relay.requests", post=upstream):
                    self.assertTrue(run_smoke(relay, "candidate"))
                self.assertEqual(len(bodies), 2)
                self.assertEqual(len(relay.smoke_verified_attempt_ids), 2)
                self.assertEqual([row["status"] for row in relay.rows], ["settled", "settled"])
            finally:
                relay.close()

    def test_strict_nonstream_does_not_expose_unsettled_tool_calls(self):
        from benchmarks.v3_deepseek import (DEEPSEEK_ENDPOINT, DEEPSEEK_PRICING,
                                            DEEPSEEK_STRICT_RELAY_CONFIG)
        from benchmarks.v3_responses_relay import ResponsesRelay
        from unittest.mock import Mock, patch

        result = {"id": "response", "model": "unexpected", "object": "chat.completion",
                  "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                      "role": "assistant", "tool_calls": [{"id": "call", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"example.txt"}'}}]}}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 3}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / "ledger.sqlite", limit_rmb="15",
                                            pricing=DEEPSEEK_PRICING)
            relay = ResponsesRelay(endpoint=DEEPSEEK_ENDPOINT, api_key="offline", budget=ledger,
                                   config=DEEPSEEK_STRICT_RELAY_CONFIG)
            relay.start()
            try:
                upstream = Mock(status_code=200, **{"json.return_value": result})
                with patch("benchmarks.v3_responses_relay.requests", post=Mock(return_value=upstream)):
                    response = requests.post(relay.url + "/chat/completions", timeout=5,
                        json={"messages": [{"role": "user", "content": "Read"}]},
                        headers={"Authorization": "Bearer " + relay.client_key})
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("tool_calls", response.json())
                self.assertEqual(ledger.campaign_calls()[0]["status"], "unknown")
            finally:
                relay.close()

    def test_stream_is_metered_only_with_finish_usage_and_done(self):
        from benchmarks.v3_deepseek import (DEEPSEEK_ENDPOINT, DEEPSEEK_PRICING,
                                            DEEPSEEK_STRICT_RELAY_CONFIG)
        from benchmarks.v3_responses_relay import ResponsesRelay
        from unittest.mock import Mock, patch

        chunk = {"id": "answer", "model": "DeepSeek-V4.1-Flash-1017", "object": "chat.completion.chunk"}
        content = {**chunk, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": "Hello"}}]}
        finish = {**chunk, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}
        usage = {**finish, "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                                      "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 3}}
        for messages, done, expected in (
            ((content, usage), True, "settled"),
            ((content, usage), False, "unknown"),
            ((content, {**usage, "id": "other"}), True, "unknown"),
            ((finish, {**usage, "choices": []}), True, "unknown"),
            ((content, finish), True, "unknown"),
        ):
            with self.subTest(messages=messages, done=done), tempfile.TemporaryDirectory() as directory:
                ledger = v3_matched.BudgetLedger(Path(directory) / "ledger.sqlite", limit_rmb="15", pricing=DEEPSEEK_PRICING)
                relay = ResponsesRelay(endpoint=DEEPSEEK_ENDPOINT, api_key="offline", budget=ledger,
                                       config=DEEPSEEK_STRICT_RELAY_CONFIG)
                upstream = Mock(status_code=200)
                frames = [b"data: " + json.dumps(message).encode() + b"\n\n" for message in messages]
                if done:
                    frames.append(b"data: [DONE]\n\n")
                upstream.iter_content.return_value = iter(frames)
                relay.start()
                try:
                    with patch("benchmarks.v3_responses_relay.requests", post=Mock(return_value=upstream)) as transport:
                        response = requests.post(relay.url + "/chat/completions", timeout=5,
                            json={"stream": True, "messages": [{"role": "user", "content": "Hello"}]},
                            headers={"Authorization": "Bearer " + relay.client_key})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"".join(frames) if expected == "settled" else
                                     (frames[0] if messages[0] == content else b""))
                    transport.post.assert_called_once()
                    self.assertEqual(transport.post.call_args.args[0], DEEPSEEK_ENDPOINT + "/beta/chat/completions")
                    sent = json.loads(transport.post.call_args.kwargs["data"])
                    self.assertEqual(sent["max_tokens"], 4096)
                    self.assertEqual(sent["model"], "deepseek-flash")
                    self.assertEqual(relay.rows[0]["status"], expected)
                    if messages == (content, usage):
                        self.assertEqual(ledger.campaign_calls()[0]["upstream_wire"],
                                         {"response_id": "answer", "finish_reason": "stop", "calls": []})
                finally:
                    relay.close()


class DeepSeekBudgetTests(unittest.TestCase):
    def test_only_model_routing_prices_pro(self):
        from benchmarks.v3_deepseek import DEEPSEEK_UNLIMITED_PRICING, DeepSeekRoutingPolicy, read_prior_exposure
        from benchmarks.v3_responses_relay import RelayConfig
        pricing = DEEPSEEK_UNLIMITED_PRICING
        flash = RelayConfig("deepseek-flash", 4096, 1000000, True)
        self.assertEqual(pricing.cost(flash.fixed_input_token_bound, flash.max_output_tokens), Decimal("2.032768"))
        with self.assertRaises(ValueError):
            pricing.for_metadata({"model": "deepseek-v4-pro", "arm": "v3"}, input_tokens=1048576)
        pro = pricing.for_metadata({"model": "deepseek-v4-pro", "arm": "routing-model-auto"},
                                  input_tokens=1048576)
        self.assertEqual(pro.cost(1048576, 4096), Decimal("9.547776"))
        policy = DeepSeekRoutingPolicy(price_date="2026-11-16")
        for arm in ("v2", "v3", "routing-default", "routing-model-simple"):
            with self.assertRaises(ValueError):
                policy.request_metadata("deepseek-v4-pro", arm)
        routing = policy.request_metadata("deepseek-v4-pro", "routing-model-auto")
        reasoning = {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "tool context"}]}
        body = {"model": "deepseek-v4-pro", "input": [reasoning]}
        self.assertEqual(policy.validate_body(body)["input"], [reasoning])
        with self.assertRaises(ValueError):
            policy.validate_body({**body, "tools": [{"type": "web_search"}]})
        response = {"status": "completed", "model": "DeepSeek-V4-Pro-0813",
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}
        self.assertIsNone(policy.response_usage("deepseek-flash", response))
        usage = policy.response_usage("deepseek-v4-pro", response)
        self.assertEqual(usage["served_model"], "DeepSeek-V4-Pro-0813")
        hosted = {**response, "usage": {**response["usage"], "server_tool_use": {"tool_search": 1, "tool_fetch": 0}}}
        self.assertIsNone(policy.response_usage("deepseek-v4-pro", hosted))
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / "combined.db", limit_rmb=None, pricing=pricing)
            self.assertTrue(ledger.reserve("flash", input_token_bound=1000000, output_token_bound=4096,
                metadata={"arm": "v3", "model": "deepseek-flash"}))
            ledger.settle("flash", None)
            self.assertTrue(ledger.reserve("pro", input_token_bound=1048576, output_token_bound=4096, metadata=routing))
            ledger.settle("pro", usage)
            account = read_prior_exposure([ledger.path])
            self.assertEqual((account.held_cny, account.settled_cny, account.attempt_count),
                             (Decimal("2.032768"), Decimal("0.00117"), 2))

    def test_immutable_pricing_is_available(self):
        self.assertIsNotNone(getattr(v3_matched, 'Pricing', None),
                             'budget pricing must be an immutable per-ledger configuration')

    def config(self):
        from benchmarks import v3_deepseek
        return v3_deepseek

    def legacy_ledger(self, path):
        module = self.config()
        ledger = v3_matched.BudgetLedger(path, limit_rmb='15', pricing=module.DEEPSEEK_PRICING)
        ledger.reserve('charged', input_token_bound=1_000_000, output_token_bound=4096,
                       metadata={'reserved_rmb': '2.032768'})
        ledger.settle('charged', {'input_tokens': 4621, 'output_tokens': 33,
                                 'cache_read_tokens': 0, 'cache_write_tokens': 0})
        ledger.reserve('unknown', input_token_bound=1_000_000, output_token_bound=4096,
                       metadata={'reserved_rmb': '2.032768'})
        ledger.settle('unknown', None)
        return ledger

    def test_cny_pricing_settlement_and_cap_are_independent_of_panstar(self):
        module = self.config()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            module.DEEPSEEK_PRICING.input_rate = Decimal('0')
        self.assertEqual(module.DEEPSEEK_PRICING.cost(1_000_000, 4096), Decimal('2.032768'))
        self.assertEqual(module.DEEPSEEK_PRICING.cost(100, 10, 20, 30), Decimal('0.0002408'))
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            pricing=module.DEEPSEEK_PRICING)
            self.assertEqual(ledger.remaining_rmb, Decimal('15'))
            with self.assertRaises(ValueError):
                v3_matched.BudgetLedger(Path(directory) / 'over.db', limit_rmb='15.000000000001',
                                       pricing=module.DEEPSEEK_PRICING)
            with self.assertRaises(ValueError):
                v3_matched.BudgetLedger(ledger.path, limit_rmb='15')

    def test_no_next_reservation_above_cutoff_and_unknown_holds_remain(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            prior_hold_rmb='12.967232000001', pricing=module.DEEPSEEK_PRICING)
            self.assertFalse(ledger.reserve('blocked', input_token_bound=1_000_000, output_token_bound=4096))
            ledger2 = v3_matched.BudgetLedger(Path(directory) / 'boundary.db', limit_rmb='15',
                                             prior_hold_rmb='12.967232', pricing=module.DEEPSEEK_PRICING)
            self.assertTrue(ledger2.reserve('last', input_token_bound=1_000_000, output_token_bound=4096))
            ledger2.settle('last', None)
            self.assertEqual(ledger2.remaining_rmb, 0)
            self.assertFalse(ledger2.reserve('retry', input_token_bound=1_000_000, output_token_bound=4096))

    def test_prior_exposure_counts_settled_and_unknown_once_across_copies(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            source, copy = Path(directory) / 'old.db', Path(directory) / 'copy.db'
            self.legacy_ledger(source)
            with sqlite3.connect(source) as original, sqlite3.connect(copy) as duplicate:
                original.backup(duplicate)
            before = source.read_bytes()
            exposure = module.read_prior_exposure([source, copy])
            self.assertEqual(exposure.settled_cny, Decimal('0.009506'))
            self.assertEqual(exposure.held_cny, Decimal('2.032768'))
            self.assertEqual(exposure.total_cny, Decimal('2.042274'))
            self.assertEqual(exposure.attempt_count, 2)
            self.assertEqual(before, source.read_bytes())
            ledger = module.create_budget(Path(directory) / 'new.db', prior_ledgers=[source, copy])
            self.assertEqual(ledger.remaining_rmb, Decimal('12.957726'))
            self.assertEqual(module.create_budget(ledger.path, prior_ledgers=[source]).remaining_rmb,
                             Decimal('12.957726'))

    def test_missing_invalid_conflicting_and_overrun_prior_fail_closed(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                module.read_prior_exposure([root / 'missing.db'])
            self.assertFalse((root / 'missing.db').exists())
            source, copy = root / 'old.db', root / 'copy.db'
            self.legacy_ledger(source)
            with sqlite3.connect(source) as original, sqlite3.connect(copy) as duplicate:
                original.backup(duplicate)
            with sqlite3.connect(copy) as db:
                db.execute("UPDATE attempts SET actual_picormb=1 WHERE id='charged'")
            with self.assertRaises(ValueError):
                module.read_prior_exposure([source, copy])
            with sqlite3.connect(copy) as db:
                db.execute("UPDATE attempts SET status='overrun' WHERE id='charged'")
            with self.assertRaises(ValueError):
                module.read_prior_exposure([copy])
            with self.assertRaises(ValueError):
                module.create_budget(root / 'new.db', prior_ledgers=[])

    def test_prior_usage_cost_is_reconstructed_instead_of_trusting_actual(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'old.db'
            self.legacy_ledger(source)
            with sqlite3.connect(source) as db:
                db.execute("UPDATE attempts SET actual_picormb=1 WHERE id='charged'")
            with self.assertRaises(ValueError):
                module.read_prior_exposure([source])

    def test_carried_prior_manifest_deduplicates_original_without_releasing_unknown(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.db'
            self.legacy_ledger(original)
            later = module.create_budget(root / 'later.db', prior_ledgers=[original])
            later.reserve('later-call', input_token_bound=1_000_000, output_token_bound=4096,
                          metadata={'reserved_rmb': '2.032768'})
            later.settle('later-call', {'input_tokens': 100, 'output_tokens': 10,
                                       'cache_read_tokens': 0, 'cache_write_tokens': 0})
            together = module.read_prior_exposure([original, later.path])
            self.assertEqual(together.total_cny, Decimal('2.042554'))
            self.assertEqual(together.held_cny, Decimal('2.032768'))
            self.assertEqual(together.attempt_count, 3)
            self.assertEqual(module.read_prior_exposure([later.path]), together)
            with sqlite3.connect(later.path) as db:
                row = db.execute("SELECT metadata_json FROM attempts WHERE id='prior-campaign-reservations'").fetchone()
                metadata = json.loads(row[0])
                metadata['prior_attempts'][0] = metadata['prior_attempts'][0].replace('charged', 'tampered')
                db.execute("UPDATE attempts SET metadata_json=? WHERE id='prior-campaign-reservations'",
                           (json.dumps(metadata),))
            with self.assertRaises(ValueError):
                module.read_prior_exposure([original, later.path])

    def test_opaque_carryover_and_underlying_original_fail_closed_in_either_order(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = v3_matched.BudgetLedger(root / 'original.db', limit_rmb='15',
                                               pricing=module.DEEPSEEK_PRICING)
            original.reserve('original-unknown', input_token_bound=1_000_000, output_token_bound=4096)
            original.settle('original-unknown', None)
            carry = v3_matched.BudgetLedger(root / 'opaque-carry.db', limit_rmb='15',
                                            prior_hold_rmb='2.032768', pricing=module.DEEPSEEK_PRICING)
            self.assertEqual(module.read_prior_exposure([carry.path]).held_cny, Decimal('2.032768'))
            for paths in ([original.path, carry.path], [carry.path, original.path]):
                with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, 'ambiguous'):
                    module.read_prior_exposure(paths)

    def test_original_opaque_hold_copy_and_manifest_keep_all_exposure_once(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, duplicate = root / 'original.db', root / 'copy.db'
            v3_matched.BudgetLedger(original, limit_rmb='15', prior_hold_rmb='0.000000000007',
                                    pricing=module.DEEPSEEK_PRICING)
            self.legacy_ledger(original)
            with sqlite3.connect(original) as source, sqlite3.connect(duplicate) as copy:
                source.backup(copy)
            expected = module.read_prior_exposure([original])
            self.assertEqual(expected.total_cny, Decimal('2.042274000007'))
            self.assertEqual(module.read_prior_exposure([original, duplicate]), expected)
            later = module.create_budget(root / 'later.db', prior_ledgers=[original])
            later.reserve('later-unknown', input_token_bound=1_000_000, output_token_bound=4096,
                          metadata={'reserved_rmb': '2.032768'})
            later.settle('later-unknown', None)
            exposure = module.read_prior_exposure([original, duplicate, later.path])
            self.assertEqual(exposure.total_cny, Decimal('4.075042000007'))
            self.assertEqual(exposure.held_cny, Decimal('4.065536000007'))
            self.assertEqual(module.read_prior_exposure([later.path]), exposure)
            unrelated = v3_matched.BudgetLedger(root / 'unrelated.db', limit_rmb='15',
                                                pricing=module.DEEPSEEK_PRICING)
            unrelated.reserve('unrelated-unknown', input_token_bound=1_000_000, output_token_bound=4096)
            unrelated.settle('unrelated-unknown', None)
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                module.read_prior_exposure([later.path, unrelated.path])

    def test_empty_original_creates_and_reopens_without_a_fake_prior_attempt(self):
        module = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = v3_matched.BudgetLedger(root / 'empty.db', limit_rmb='15', pricing=module.DEEPSEEK_PRICING)
            exposure = module.read_prior_exposure([empty.path])
            self.assertEqual(exposure.records, ())
            self.assertEqual(exposure.total_cny, 0)
            for _ in range(2):
                ledger = module.create_budget(root / 'new.db', prior_ledgers=[empty.path])
                self.assertEqual(ledger.remaining_rmb, Decimal('15'))
                with sqlite3.connect(ledger.path) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
            nonempty_records = (json.dumps({'id': 'unproven'}),)
            with self.assertRaises(ValueError):
                v3_matched.BudgetLedger(ledger.path, limit_rmb='15', pricing=module.DEEPSEEK_PRICING,
                                        prior_hold_rmb='0', prior_hold_records=nonempty_records)


class DeepSeekRelayTests(unittest.TestCase):
    def test_deepseek_budget_rejects_default_provider_or_undersized_reservation(self):
        from benchmarks.v3_deepseek import (DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG,
                                            DEEPSEEK_UNLIMITED_PRICING, DeepSeekRoutingPolicy)
        from benchmarks.v3_panstar import PanstarRelay
        from benchmarks.v3_responses_relay import RelayConfig, ResponsesRelay
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            pricing=DEEPSEEK_PRICING)
            for config in (RelayConfig(), RelayConfig(model='deepseek-flash'),
                           RelayConfig(model='deepseek-flash', fixed_input_token_bound=999_999, serial_requests=True)):
                with self.subTest(config=config), self.assertRaises(ValueError):
                    ResponsesRelay(endpoint='http://127.0.0.1:1', api_key='fake', budget=ledger, config=config)
            unlimited = v3_matched.BudgetLedger(Path(directory) / 'unlimited.db', limit_rmb=None,
                                               pricing=DEEPSEEK_UNLIMITED_PRICING)
            fixed = ResponsesRelay(endpoint='https://api.deepseek.com', api_key='fake',
                                   budget=unlimited, config=DEEPSEEK_RELAY_CONFIG)
            self.addCleanup(fixed._server.server_close)
            self.assertEqual(fixed.issue_actor_config('root')['model_complex'], 'deepseek-flash')
            roles = PanstarRelay(endpoint='http://127.0.0.1:1', api_key='fake', budget=unlimited,
                                 policy=DeepSeekRoutingPolicy())
            self.addCleanup(roles._server.server_close)
            self.assertEqual(roles.issue_actor_config('root')['model_complex'], 'deepseek-v4-pro')

    def test_fixed_context_reservation_model_and_serial_stream_lifecycle(self):
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG
        from benchmarks.v3_responses_relay import ResponsesRelay
        received, balances = [], []
        first_entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        ledger_ref = {}

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                balances.append(ledger_ref['ledger'].remaining_rmb)
                if len(received) == 1:
                    first_entered.set()
                else:
                    second_entered.set()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if len(received) == 1:
                    release.wait(5)
                result = {'type': 'response.completed', 'response': {'usage': {
                    'input_tokens': 100, 'output_tokens': 10,
                    'input_tokens_details': {'cached_tokens': 20}, 'cache_creation_input_tokens': 30}}}
                self.wfile.write(('data: ' + json.dumps(result) + '\n\n').encode())

        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            pricing=DEEPSEEK_PRICING)
            ledger_ref['ledger'] = ledger
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            relay = ResponsesRelay(endpoint=f'http://127.0.0.1:{upstream.server_port}',
                                   api_key='fake-key', budget=ledger, config=DEEPSEEK_RELAY_CONFIG)
            relay.start()
            results = []

            def request():
                results.append(requests.post(relay.url + '/responses', json={'input': 'x', 'stream': True},
                                             headers={'Authorization': 'Bearer ' + relay.client_key}, timeout=8))

            first, second = threading.Thread(target=request), threading.Thread(target=request)
            try:
                first.start()
                self.assertTrue(first_entered.wait(2))
                second.start()
                self.assertFalse(second_entered.wait(.2))
                release.set()
                first.join(5)
                second.join(5)
                self.assertEqual([r.status_code for r in results], [200, 200])
                self.assertEqual(received[0]['model'], 'deepseek-flash')
                self.assertEqual(received[0]['max_output_tokens'], 4096)
                self.assertEqual(balances[0], Decimal('12.967232'))
                self.assertEqual(ledger.spent_rmb, Decimal('0.0004816'))
                self.assertEqual([r['reserved_rmb'] for r in relay.rows], ['2.032768', '2.032768'])
            finally:
                release.set()
                first.join(5)
                if second.ident:
                    second.join(5)
                relay.close()
                upstream.shutdown()
                upstream.server_close()
                upstream_thread.join()


class DeepSeekRunnerTests(unittest.TestCase):
    def test_runner_reports_cny_and_uses_configured_model_without_fx_conversion(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG
        from unittest.mock import patch
        commands, relay_options = [], []

        from tests.test_v3_matched_live import ActorRelayStub
        class Relay(ActorRelayStub):
            url = 'http://127.0.0.1:1/v1'
            client_key = 'local'
            provider_error = None
            budget_blocked = False
            rows = []

            def __init__(self, **kwargs):
                relay_options.append(kwargs)

            def start(self):
                pass

            def close(self):
                pass

        def worker(command, **kwargs):
            commands.append(command)
            from tests.test_v3_matched_live import valid_worker_result
            Path(command[command.index('--result') + 1]).write_text(json.dumps(valid_worker_result()))
            return 0, ''

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            from tests.test_v3_matched_live import prepare_candidate_apparatus
            prepare_candidate_apparatus(root / 'src')
            with patch.object(live, '_source_snapshot', return_value='candidate'), \
                    patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}), \
                    patch('benchmarks.v3_deepseek.run_smoke', return_value=True), \
                    patch('benchmarks.v3_deepseek.run_planning_preflight', return_value={'status': 'passed'}), \
                    patch('openkyrozen.tools.sandbox.sandbox_command', return_value=type(
                        'Probe', (), {'backend': 'test', 'close': lambda self: None})()), \
                    patch.object(live, '_credential', return_value=('https://wrong.invalid', 'fake-key')), \
                    patch.object(live, 'ResponsesRelay', Relay), \
                    patch.object(live, '_extract_baseline'), \
                    patch.object(live, '_run_worker', side_effect=worker), \
                    patch('builtins.print'), \
                    patch.object(live, 'verify_fixture', return_value={
                        'result': 'failed', 'safety_passed': False,
                        'safety_components': {'scope_safe': False, 'original_tests_unchanged': True,
                                              'provider_ok': True, 'no_running_processes': True},
                        'plan_scenario_passed': False, 'verified_success': False, 'changed_paths': []}):
                report = live.run(source=root / 'src', repo=root, output=root / 'report.json',
                                  budget_path=root / 'budget.db', limit_cny='15',
                                  prior_hold_cny='0.582914960007', pricing=DEEPSEEK_PRICING,
                                  relay_config=DEEPSEEK_RELAY_CONFIG, endpoint='https://api.deepseek.com')
            self.assertEqual(report['budget']['limit_cny'], '15')
            self.assertEqual(report['budget']['prior_campaign_hold_cny'], '0.582914960007')
            self.assertEqual(report['budget']['remaining_cny'], '14.417085039993')
            self.assertNotIn('usd_cny', report['budget'])
            self.assertNotIn('limit_usd', report['budget'])
            self.assertEqual(report['model'], 'deepseek-flash')
            self.assertEqual(report['pricing']['source_currency'], 'CNY')
            self.assertEqual(commands[0][commands[0].index('--model') + 1], 'deepseek-flash')
            self.assertEqual(relay_options[0]['endpoint'], 'https://api.deepseek.com')
            self.assertEqual(report['status'], 'blocked_safety_failure')

    def test_cli_reconstructs_prior_ledger_and_checks_configured_signed_candidate(self):
        from benchmarks import v3_deepseek as deepseek
        from unittest.mock import patch
        summary = {'complete': False, 'accounting_complete': False, 'comparison_thresholds_met': False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / 'prior.db'
            DeepSeekBudgetTests().legacy_ledger(prior)
            argv = ['deepseek', '--source', str(root / 'src'), '--candidate-sha', 'configured-sha',
                    '--repo', str(root), '--output', str(root / 'report.json'),
                    '--budget-db', str(root / 'new.db'), '--prior-ledger', str(prior)]
            with patch('sys.argv', argv), patch.dict('os.environ', {'KYROZEN_BENCHMARK_API_KEY': 'fake-env-key'}), \
                    patch('benchmarks.v3_matched_live._source_snapshot', return_value='configured-sha'), \
                    patch('scripts.publish_v3_preview._verify_candidate_signature') as signature, \
                    patch('benchmarks.v3_matched_live.run', return_value={'summary': summary}) as runner, \
                    patch('builtins.print'):
                with self.assertRaises(SystemExit) as result:
                    deepseek.main()
                self.assertEqual(result.exception.code, 1)
                signature.assert_called_once_with((root / 'src').resolve(), 'configured-sha')
                self.assertEqual(runner.call_args.kwargs['prior_hold_cny'], '2.042274')
                self.assertEqual(runner.call_args.kwargs['limit_cny'], '15')
                self.assertEqual(runner.call_args.kwargs['endpoint'], 'https://api.deepseek.com')
            for status, code in (('planning_preflight_passed', 0), ('blocked_planning_preflight', 1)):
                with patch('sys.argv', argv + ['--planning-preflight-only']), \
                        patch('benchmarks.v3_matched_live._source_snapshot', return_value='configured-sha'), \
                        patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                        patch('benchmarks.v3_matched_live.run', return_value={
                            'status': status, 'summary': summary, 'budget': {}, 'planning_preflight': {}}) as runner, \
                        patch('builtins.print'):
                    with self.assertRaises(SystemExit) as result:
                        deepseek.main()
                    self.assertEqual(result.exception.code, code)
                    self.assertTrue(runner.call_args.kwargs['planning_preflight_only'])
                    self.assertFalse(summary['complete'])
            with patch('sys.argv', argv + ['--limit-cny', 'unlimited']), \
                    patch('benchmarks.v3_matched_live._source_snapshot', return_value='configured-sha'), \
                    patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                    patch.object(deepseek, 'read_prior_exposure', return_value=deepseek.PriorExposure(
                        Decimal('6'), Decimal('121'), 1)), \
                    patch('benchmarks.v3_matched_live.run', return_value={'summary': summary}) as runner, \
                    patch('builtins.print'):
                with self.assertRaises(SystemExit):
                    deepseek.main()
                options = runner.call_args.kwargs
                self.assertEqual(options['prior_hold_cny'], '127')
                self.assertEqual(options['limit_cny'], 'unlimited')
                self.assertIsNone(options['pricing'].max_limit_cny)
                self.assertNotEqual(options['pricing'].sha256, deepseek.DEEPSEEK_PRICING.sha256)
            with patch('sys.argv', argv + ['--limit-cny', '15.01']), \
                    patch('benchmarks.v3_matched_live._source_snapshot', return_value='configured-sha'), \
                    patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                    patch('benchmarks.v3_matched_live.run') as runner:
                with self.assertRaises(ValueError):
                    deepseek.main()
                runner.assert_not_called()

    def test_fresh_runner_accepts_only_same_candidate_smoke_in_shared_ledger(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG
        from unittest.mock import patch
        for candidate, model, carryover, accepted in (
                ('candidate', 'deepseek-flash', '0.582914960007', True),
                ('other-candidate', 'deepseek-flash', '0.582914960007', False),
                ('candidate', 'wrong-model', '0.582914960007', False),
                ('candidate', 'deepseek-flash', '0.1', False)):
            with self.subTest(candidate=candidate, model=model, carryover=carryover), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                budget_path = root / 'budget.db'
                from tests.test_v3_matched_live import prepare_candidate_apparatus
                prepare_candidate_apparatus(root / 'src')
                ledger = v3_matched.BudgetLedger(budget_path, limit_rmb='15', pricing=DEEPSEEK_PRICING,
                                                prior_hold_rmb='0.582914960007')
                ledger.reserve('smoke', input_token_bound=1_000_000, output_token_bound=4096,
                               metadata={'arm': 'smoke', 'fixture_id': candidate, 'repeat': -1,
                                         'model': model, 'max_output_tokens': 4096, 'reserved_rmb': '2.032768'})
                ledger.settle('smoke', {'input_tokens': 100, 'output_tokens': 10,
                                        'cache_read_tokens': 0, 'cache_write_tokens': 0})
                with patch.object(live, '_source_snapshot', return_value='candidate'), \
                        patch('openkyrozen.tools.sandbox.sandbox_command', return_value=type(
                            'Probe', (), {'backend': 'test', 'close': lambda self: None})()), \
                        patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}), \
                        patch.object(live, '_credential', return_value=('https://invalid.example', 'fake-key')), \
                        patch.object(live, 'ResponsesRelay', return_value=type('Relay', (), {'rows': []})()), \
                        patch.object(live, '_extract_baseline', side_effect=RuntimeError('after-preflight')):
                    if accepted:
                        with self.assertRaisesRegex(RuntimeError, 'after-preflight'):
                            live.run(source=root / 'src', repo=root, output=root / 'report.json',
                                     budget_path=budget_path, limit_cny='15', prior_hold_cny=carryover,
                                     pricing=DEEPSEEK_PRICING, relay_config=DEEPSEEK_RELAY_CONFIG)
                    else:
                        with self.assertRaises(ValueError):
                            live.run(source=root / 'src', repo=root, output=root / 'report.json',
                                     budget_path=budget_path, limit_cny='15', prior_hold_cny=carryover,
                                     pricing=DEEPSEEK_PRICING, relay_config=DEEPSEEK_RELAY_CONFIG)
                self.assertEqual(ledger.spent_rmb, Decimal('0.00028'))

    def test_resume_rejects_changed_model_or_frozen_relay_configuration_before_credentials(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG
        from unittest.mock import patch
        for changed in ('model', 'relay_config'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ledger_path = root / 'budget.db'
                v3_matched.BudgetLedger(ledger_path, limit_rmb='15', prior_hold_rmb='0.5', pricing=DEEPSEEK_PRICING)
                checkpoint = {**live.apparatus_identity(), 'protocol': 'openkyrozen-v3-matched-live-v1', 'candidate_sha': 'candidate',
                              'baseline_sha': live.BASELINE, 'model': 'deepseek-flash', 'repeats': 3,
                              'pricing': DEEPSEEK_PRICING.as_dict(),
                              'relay_config': dataclasses.asdict(DEEPSEEK_RELAY_CONFIG),
                              'budget': {'prior_campaign_hold_cny': '0.5'}, 'rows': [], 'calls': []}
                if changed == 'model':
                    checkpoint['model'] = 'another-model'
                else:
                    checkpoint['relay_config']['serial_requests'] = False
                live._write_checkpoint(root / 'report.json', ledger_path, checkpoint)
                with patch.object(live, '_source_snapshot', return_value='candidate'), \
                        patch('openkyrozen.tools.sandbox.sandbox_command', return_value=type(
                            'Probe', (), {'backend': 'test', 'close': lambda self: None})()), \
                        patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}), \
                        patch.object(live, '_credential') as credential:
                    with self.assertRaisesRegex(ValueError, 'checkpoint does not match'):
                        live.run(source=root / 'src', repo=root, output=root / 'report.json',
                                 budget_path=ledger_path, limit_cny='15', prior_hold_cny='0.5',
                                 pricing=DEEPSEEK_PRICING, relay_config=DEEPSEEK_RELAY_CONFIG)
                    credential.assert_not_called()


class PlanningPreflightTests(unittest.TestCase):
    def runner(self, root, worker):
        from contextlib import ExitStack
        from unittest.mock import patch
        from benchmarks import v3_matched_live as live
        from benchmarks import v3_deepseek as deepseek
        from tests.test_v3_matched_live import ActorRelayStub, prepare_candidate_apparatus
        prepare_candidate_apparatus(root / 'source')
        state = {}
        class Relay(ActorRelayStub):
            url, client_key = 'http://127.0.0.1:1/v1', 'fake-local'
            provider_error, budget_blocked = None, False
            def __init__(self, **options):
                self.rows, self.budget, self.config = [], options['budget'], options['config']
                self._upstream_lock = threading.Lock()
                self.planning_preflight_closed = False
                state['relay'] = self
            def start(self):
                pass
            def close(self):
                pass
        def charge(relay, arm, prefix, count):
            relay.label = (arm, 'candidate', -1)
            for number in range(count):
                row = {'attempt_id': prefix + str(number), 'arm': arm, 'fixture_id': 'candidate',
                    'repeat': -1, 'model': 'deepseek-flash', 'max_output_tokens': 4096,
                    'reserved_rmb': '2.032768'}
                self.assertTrue(relay.budget.reserve(row['attempt_id'], input_token_bound=1_000_000,
                    output_token_bound=4096, metadata=row))
                usage = {'input_tokens': 100, 'output_tokens': 10, 'cache_read_tokens': 0, 'cache_write_tokens': 0}
                relay.budget.settle(row['attempt_id'], usage)
                relay.rows.append({**row, 'status': 'settled', 'usage': usage,
                    'actual_rmb': str(relay.budget.cost(100, 10, 0, 0))})
        def smoke(relay, candidate):
            if getattr(relay, 'smoke_verified_candidate', None) != candidate:
                charge(relay, 'smoke', 'smoke-', 2)
                relay.smoke_verified_candidate = candidate
                relay.smoke_verified_attempt_ids = ('smoke-0', 'smoke-1')
            return True
        def execute(command, **options):
            self.assertEqual(state['relay'].label, ('planning_preflight', 'candidate', -1))
            charge(state['relay'], 'planning_preflight', 'planning-', 4)
            Path(command[command.index('--result') + 1]).write_text(json.dumps(worker))
            return 0, ''
        stack = ExitStack()
        for mocked in (patch.object(live, '_source_snapshot', return_value='candidate'),
            patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}),
            patch('openkyrozen.tools.sandbox.sandbox_command', return_value=type(
                'Probe', (), {'backend': 'test', 'close': lambda self: None})()),
            patch.object(live, '_credential', return_value=('http://invalid.example', 'fake-key')),
            patch.object(live, 'ResponsesRelay', Relay), patch.object(live, '_extract_baseline'),
            patch.object(live, '_run_worker', side_effect=execute),
            patch.object(deepseek, 'run_smoke', side_effect=smoke)):
            stack.enter_context(mocked)
        options = dict(source=root / 'source', repo=root, output=root / 'core.json',
            budget_path=root / 'budget.sqlite3', limit_cny='15', prior_hold_cny='6.815266400007',
            pricing=deepseek.DEEPSEEK_PRICING, relay_config=deepseek.DEEPSEEK_RELAY_CONFIG)
        return stack, options

    def native_worker(self, candidate='candidate', *, retrieve_artifact=False, confirmation_guidance=False):
        """Real native tools and durable plan state; only the model is scripted."""
        from contextlib import ExitStack, closing
        from unittest.mock import patch
        import os
        import time
        from benchmarks import v3_deepseek as deepseek
        from benchmarks.v3_matched_live import _fixture_prompt
        from benchmarks.prompt_worker import _normalize_tool_receipt, _task_record
        from tests.test_v3_matched_live import valid_worker_result
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers import ProviderConfig
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.models import (FinishReason, ModelResponse, ProviderCapabilities,
            ToolCall, ToolReply, provider_continuation)
        from openkyrozen.agent.events import runtime_event_as_legacy
        from openkyrozen.tools import ToolAdapters
        with ExitStack() as owners:
            home = Path(owners.enter_context(tempfile.TemporaryDirectory()))
            workspace = home / 'workspace'
            workspace.mkdir()
            owners.enter_context(patch.dict(os.environ, {'HOME': str(home),
                'KYROZEN_WORKSPACE_ROOT': str(workspace), 'KYROZEN_DISABLE_VECTOR_INDEX': '1',
                'KYROZEN_CLI_CAPABILITIES': 'read,write', 'KYROZEN_APPROVAL_MODE': 'never'}))
            memory = owners.enter_context(closing(build_memory(home / 'state.sqlite3', index=None)))
            app = build_application(surface='cli', memory=memory, tools=ToolAdapters(workspace))
            owners.callback(app.close)
            runtime = app.runtime
            runtime._set_workspace_root(workspace)
            runtime.set_interaction_mode('agent')
            (workspace / 'proof.txt').write_text(deepseek.PLANNING_PREFLIGHT_CONTENT)
            case = self
            class Provider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, *_args, **_kwargs):
                    raise AssertionError('legacy model API used')
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    if self.calls == 1:
                        args = {'complexity': 'simple', 'expected_revision': 0, 'operations': [
                            {'op': 'add', 'ref': 'proof', 'task': deepseek.planning_preflight_task()},
                            {'op': 'start', 'task': 'proof'}]}
                        if confirmation_guidance:
                            args['reason'] = 'Verify the planning preflight'
                    elif retrieve_artifact and self.calls == 3:
                        if 'read_tool_artifact' not in {item['name'] for item in tools}:
                            raise AssertionError('archived result reader is not provider-visible')
                        # Model an earlier archived reply using the real scoped artifact store.
                        from openkyrozen.agent.engine import AgentEngine
                        index = next(i for i, item in enumerate(messages)
                                     if isinstance(item, ToolReply) and item.call_id == 'plan-1')
                        original = json.dumps(messages[index].output, ensure_ascii=False,
                                              sort_keys=True, separators=(',', ':'))
                        AgentEngine(runtime)._archive_replies(messages, [index])
                        archived = next(item for item in messages if isinstance(item, ToolReply) and item.call_id == 'plan-1')
                        page = memory.store.artifacts.read_page(archived.output['data']['artifact_id'],
                            0, len(original), user_id=runtime.tasks.user_id, workspace_id=runtime.tasks.workspace_id,
                            session_id=runtime.tasks.session_id)
                        case.assertEqual(page['content'], original)
                        case.assertEqual(page['total_chars'], len(original))
                        return ModelResponse(tool_calls=(ToolCall('archive', 'read_tool_artifact',
                            {'artifact_id': archived.output['data']['artifact_id']}),),
                            finish_reason=FinishReason.TOOL_REQUEST,
                            continuation=provider_continuation(self, model, 'fixture', {'step': self.calls}))
                    elif self.calls == (4 if retrieve_artifact else 3):
                        state = self.plan_state(messages)
                        if state['status'] != 'completed' or state['tasks'][0]['status'] != 'succeeded':
                            raise AssertionError('verified completed state is not provider-visible')
                        args = {'plan_id': state['plan_id'], 'expected_revision': state['revision'],
                            'operations': [{'op': 'suggest_complete', 'task': state['tasks'][0]['id']}]}
                        if confirmation_guidance:
                            import re
                            # A client copying the published snapshot needs explicit omission guidance.
                            args['reason'] = state['reason']
                            pin = next(item['content'] for item in messages if isinstance(item, dict)
                                       and item.get('content', '').startswith('## Pinned Working Plan state\n'))
                            tool = next(item for item in tools if item['name'] == 'update_plan')
                            reason = tool['parameters']['properties']['reason'].get('description', '')
                            fields = re.search(r'ONLY (plan_id, expected_revision, operations)', pin)
                            if fields and 'omit reason' in tool['description'].lower() and 'omit' in reason.lower():
                                args = {key: args[key] for key in fields[1].split(', ')}
                    elif self.calls == 2:
                        if self.plan_state(messages)['tasks'][0]['status'] != 'running':
                            raise AssertionError('unverified task completed before reading')
                        return ModelResponse(tool_calls=(ToolCall('read', 'read_file', {'path': 'proof.txt'}),),
                            finish_reason=FinishReason.TOOL_REQUEST,
                            continuation=provider_continuation(self, model, 'fixture', {'step': self.calls}))
                    else:
                        return ModelResponse(text=deepseek.PLANNING_PREFLIGHT_CONTENT.strip(), finish_reason=FinishReason.FINAL)
                    return ModelResponse(tool_calls=(ToolCall('plan-' + str(self.calls), 'update_plan', args),),
                        finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, 'fixture', {'step': self.calls}))
                def plan_state(self, messages):
                    pin = next(item['content'] for item in messages if isinstance(item, dict)
                               and item.get('content', '').startswith('## Pinned Working Plan state\n'))
                    return json.loads(pin.split('\n', 2)[2])
            provider = Provider(ProviderConfig(provider='custom', model_simple='fixture'))
            runtime.llm_provider = runtime._default_turn.provider = provider
            def prepare(turn):
                turn.interaction_mode, turn.complexity = 'agent', 'complex'
                turn.learning_run, turn.turn_start, turn.learned_context = {'run_id': 'fixture'}, time.time(), ''
            events = []
            from contextlib import redirect_stdout
            import io
            with redirect_stdout(io.StringIO()), patch.object(runtime, '_prepare_turn', side_effect=prepare), \
                    patch.object(runtime, '_finish_learning_run', side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                    patch.object(runtime, '_schedule_post_turn_learning'):
                prompt = _fixture_prompt(deepseek.planning_preflight_fixture(candidate))
                answer = runtime.chat(runtime.current_session, prompt,
                                      on_runtime_event=events.append)
            legacy = [runtime_event_as_legacy(event) or {} for event in events]
            receipts = [_normalize_tool_receipt(event['tool_receipt'], workspace)
                        for event in legacy if event.get('event') == 'tool_receipt']
            self.assertEqual(provider.calls, 5 if retrieve_artifact else 4, answer)
            return valid_worker_result(answer=answer, tool_receipts=receipts, events=legacy,
                conversation_records=[{'turn': 0, 'prompt_sha256': __import__('hashlib').sha256(prompt.encode()).hexdigest(),
                                       'completed': True}],
                tasks=[task['status'] for task in runtime.tasks.tasks],
                task_records=[_task_record(task) for task in runtime.tasks.tasks],
                working_plan=runtime.tasks.working_plan_snapshot())

    def test_real_native_explicit_start_typed_read_then_confirmation_completes(self):
        from benchmarks.v3_deepseek import planning_preflight_semantics
        worker = self.native_worker()
        self.assertTrue(planning_preflight_semantics(worker), worker)
        self.assertEqual(worker['working_plan']['status'], 'completed')

    def test_published_completion_guidance_omits_snapshot_reason_on_first_confirmation(self):
        from benchmarks.v3_deepseek import planning_preflight_semantics
        worker = self.native_worker(retrieve_artifact=True, confirmation_guidance=True)
        self.assertTrue(planning_preflight_semantics(worker), worker)
        confirmations = [item['evidence'] for item in worker['tool_receipts']
                         if item['action'] == 'update_plan'
                         and (item['evidence'].get('verification') or {}).get('method') == 'working_plan_confirmation']
        self.assertEqual(len(confirmations), 1)
        arguments = confirmations[0].get('arguments', confirmations[0].get('args'))
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        self.assertEqual(set(arguments), {'plan_id', 'expected_revision', 'operations'})

    def test_native_scoped_archive_read_preserves_all_three_preflight_proofs(self):
        from copy import deepcopy
        from benchmarks.v3_deepseek import planning_preflight_semantics
        worker = self.native_worker(retrieve_artifact=True)
        self.assertTrue(planning_preflight_semantics(worker), worker)
        archive = next(item['evidence'] for item in worker['tool_receipts'] if item['action'] == 'read_tool_artifact')
        self.assertTrue(archive['success'])
        for field, value in (('authorized', False), ('success', False), ('user_id', 'foreign'),
                             ('workspace_id', 'foreign'), ('session_id', 'foreign'), ('side_effects', 'filesystem')):
            bad = deepcopy(worker)
            changed = next(item['evidence'] for item in bad['tool_receipts'] if item['action'] == 'read_tool_artifact')
            changed[field] = value
            with self.subTest(field=field):
                self.assertFalse(planning_preflight_semantics(bad))
        bad = deepcopy(worker)
        bad['tool_receipts'] = [item for item in bad['tool_receipts']
            if (item['evidence'].get('verification') or {}).get('method') != 'working_plan_confirmation']
        self.assertFalse(planning_preflight_semantics(bad))  # Exact360failure shape: succeeded, but never confirmed.

    def test_status_prose_unbound_wrong_content_or_any_effect_cannot_pass(self):
        from copy import deepcopy
        from benchmarks.v3_deepseek import planning_preflight_semantics
        worker = self.native_worker()
        changes = ('missing_start', 'unbound', 'wrong_content', 'no_suggestion', 'write_then_restore', 'empty_plan',
            'predicate', 'task_status', 'dependencies', 'legacy', 'confirmation_status', 'confirmation_scope',
            'confirmation_turn', 'missing_proof', 'malformed_proof')
        for change in changes:
            bad = deepcopy(worker)
            read = next(item['evidence'] for item in bad['tool_receipts'] if item['action'] == 'read_file')
            if change == 'unbound':
                read['task_id'] = None
            elif change == 'wrong_content':
                read['result_data']['content'] = 'wrong'
            elif change == 'missing_start':
                arguments = json.loads(bad['tool_receipts'][0]['evidence']['args'])
                arguments['operations'].pop()
                bad['tool_receipts'][0]['evidence']['args'] = json.dumps(arguments)
            elif change == 'no_suggestion':
                bad['tool_receipts'].pop(2)
            elif change == 'empty_plan':
                bad['working_plan']['tasks'] = []
            elif change == 'predicate':
                bad['task_records'][0]['acceptance'][0]['expect']['equals'] = 'wrong'
            elif change == 'task_status':
                bad['task_records'][0]['status'] = 'failed'
            elif change == 'dependencies':
                bad['task_records'][0]['dependencies'] = ['foreign']
            elif change == 'legacy':
                read['call_id_source'] = 'legacy'
            elif change == 'confirmation_status':
                bad['tool_receipts'][2]['evidence']['verification']['status'] = 'not_applicable'
            elif change == 'confirmation_scope':
                bad['tool_receipts'][2]['evidence']['verification']['observed']['plan_id'] = 'foreign'
            elif change == 'confirmation_turn':
                bad['tool_receipts'][2]['evidence']['verification']['observed']['turn_id'] = 'foreign'
            elif change == 'missing_proof':
                bad['task_records'][0]['evidence'] = None
            elif change == 'malformed_proof':
                bad['task_records'][0]['checkpoint'] = 'not proof'
            else:
                bad['tool_receipts'].append({'action': 'write_file', 'evidence': {'side_effects': 'filesystem'}})
            with self.subTest(change=change):
                self.assertFalse(planning_preflight_semantics(bad))

    def test_preflight_only_same_ledger_then_resume_does_not_repeat_or_add_formal_rows(self):
        from benchmarks import v3_matched_live as live
        from unittest.mock import patch
        worker = self.native_worker()
        with tempfile.TemporaryDirectory() as directory:
            stack, options = self.runner(Path(directory), worker)
            with stack:
                report = live.run(**options, planning_preflight_only=True)
                self.assertEqual(report['status'], 'planning_preflight_passed')
                self.assertEqual(report['rows'], [])
                self.assertFalse(report['summary']['complete'])
                self.assertEqual(report['planning_preflight']['status'], 'passed')
                self.assertEqual(len(report['smoke_attempt_ids']), 2)
                self.assertEqual(len(report['planning_preflight']['attempt_ids']), 4)
                self.assertEqual(len(report['calls']), 6)
                with patch.object(live, '_execute_cell', side_effect=RuntimeError('formal-start')) as execute:
                    with self.assertRaisesRegex(RuntimeError, 'formal-start'):
                        live.run(**options)
                    self.assertIn(execute.call_args.kwargs['arm'], ('v2', 'v3'))
                db = sqlite3.connect(options['budget_path'])
                try:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts WHERE id!='prior-campaign-reservations'").fetchone()[0], 6)
                finally:
                    db.close()

    def test_resume_rejects_preflight_lies_missing_evidence_or_extra_calls_before_key(self):
        from copy import deepcopy
        from benchmarks import v3_matched_live as live
        from unittest.mock import patch
        worker = self.native_worker()
        with tempfile.TemporaryDirectory() as directory:
            stack, options = self.runner(Path(directory), worker)
            with stack:
                original = live.run(**options, planning_preflight_only=True)
                for change in ('failed', 'interrupted', 'fixture', 'extra-id', 'missing-evidence'):
                    report = deepcopy(original)
                    phase = report['planning_preflight']
                    if change in {'failed', 'interrupted'}:
                        phase['status'] = change
                    elif change == 'fixture':
                        phase['fixture_sha256'] = '0' * 64
                    elif change == 'extra-id':
                        phase['attempt_ids'].append('forged')
                    else:
                        phase['row']['worker_evidence_sha256'] = '0' * 64
                    live._write_checkpoint(options['output'], options['budget_path'], report)
                    with self.subTest(change=change), patch.object(live, '_credential') as credential:
                        with self.assertRaises(ValueError):
                            live.run(**options)
                        credential.assert_not_called()

    def test_preflight_requires_complete_usage_actual_charge_and_exact_row_totals(self):
        from copy import deepcopy
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import validate_planning_preflight
        worker = self.native_worker()
        with tempfile.TemporaryDirectory() as directory:
            stack, options = self.runner(Path(directory), worker)
            with stack:
                report = live.run(**options, planning_preflight_only=True)
                for change in ('empty', 'bool', 'negative', 'cache', 'actual', 'tokens', 'attempts'):
                    record, calls = deepcopy(report['planning_preflight']), deepcopy(report['calls'])
                    call = next(item for item in calls if item['arm'] == 'planning_preflight')
                    if change == 'empty':
                        call['usage'] = {}
                    elif change == 'bool':
                        call['usage']['input_tokens'] = True
                    elif change == 'negative':
                        call['usage']['output_tokens'] = -1
                    elif change == 'cache':
                        call['usage']['cache_read_tokens'] = 101
                    elif change == 'actual':
                        call['actual_rmb'] = '0'
                    elif change == 'tokens':
                        record['row']['input_tokens'] += 1
                    else:
                        record['row']['attempts'] += 1
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        validate_planning_preflight(options['output'], record, calls, 'candidate', live.apparatus_identity())

    def test_failed_or_interrupted_preflight_stops_and_preserves_uncheckpointed_hold(self):
        from copy import deepcopy
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING
        from unittest.mock import patch
        worker = self.native_worker()
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                bad = deepcopy(worker)
                bad['task_records'][0]['acceptance'] = None
                stack, options = self.runner(Path(directory), bad)
                with stack:
                    if interrupted:
                        def stop(**_options):
                            ledger = v3_matched.BudgetLedger(options['budget_path'], limit_rmb='15',
                                prior_hold_rmb=options['prior_hold_cny'], pricing=DEEPSEEK_PRICING)
                            ledger.reserve('inflight', input_token_bound=1_000_000, output_token_bound=4096,
                                metadata={'arm': 'planning_preflight', 'fixture_id': 'candidate', 'repeat': -1,
                                          'model': 'deepseek-flash', 'max_output_tokens': 4096,
                                          'reserved_rmb': '2.032768'})
                            raise KeyboardInterrupt()
                        with patch.object(live, '_execute_cell', side_effect=stop):
                            with self.assertRaises(KeyboardInterrupt):
                                live.run(**options)
                    else:
                        report = live.run(**options)
                        self.assertEqual(report['status'], 'blocked_planning_preflight')
                        self.assertEqual(report['rows'], [])
                        self.assertEqual(report['planning_preflight']['status'], 'failed')
                    with patch.object(live, '_credential') as credential:
                        with self.assertRaises(ValueError):
                            live.run(**options)
                        credential.assert_not_called()
                    if interrupted:
                        db = sqlite3.connect(options['budget_path'])
                        try:
                            self.assertEqual(db.execute('SELECT status,reserved_picormb,actual_picormb,usage_json '
                                'FROM attempts WHERE id=\'inflight\'').fetchone(),
                                ('unknown', 2032768000000, None, None))
                        finally:
                            db.close()

    def test_uncheckpointed_extra_preflight_journal_call_blocks_resume_before_key(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING
        from unittest.mock import patch
        worker = self.native_worker()
        with tempfile.TemporaryDirectory() as directory:
            stack, options = self.runner(Path(directory), worker)
            with stack:
                live.run(**options, planning_preflight_only=True)
                ledger = v3_matched.BudgetLedger(options['budget_path'], limit_rmb='15',
                    prior_hold_rmb=options['prior_hold_cny'], pricing=DEEPSEEK_PRICING)
                ledger.reserve('extra', input_token_bound=1_000_000, output_token_bound=4096,
                    metadata={'arm': 'planning_preflight', 'fixture_id': 'candidate', 'repeat': -1,
                              'model': 'deepseek-flash', 'max_output_tokens': 4096, 'reserved_rmb': '2.032768'})
                ledger.settle('extra', {'input_tokens': 100, 'output_tokens': 10,
                                       'cache_read_tokens': 0, 'cache_write_tokens': 0})
                with patch.object(live, '_credential') as credential:
                    with self.assertRaisesRegex(ValueError, 'checkpoint or journal'):
                        live.run(**options)
                    credential.assert_not_called()

    def test_phase_completion_drains_already_admitted_request_before_success(self):
        from copy import deepcopy
        from unittest.mock import patch
        from benchmarks import v3_deepseek as deepseek, v3_matched_live as live
        from benchmarks.v3_responses_relay import ResponsesRelay
        worker = self.native_worker()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack, options = self.runner(root, worker)
            with stack:
                report = live.run(**options, planning_preflight_only=True)
            budget = v3_matched.BudgetLedger(root / 'admitted.sqlite3', limit_rmb='15', pricing=deepseek.DEEPSEEK_PRICING)
            for call in report['calls']:
                self.assertTrue(budget.reserve(call['attempt_id'], input_token_bound=1_000_000,
                    output_token_bound=4096, metadata=call))
                budget.settle(call['attempt_id'], call['usage'])
            received = []
            class Upstream(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass
                def do_POST(self):
                    received.append(self.rfile.read(int(self.headers['Content-Length'])))
                    body = json.dumps({'id': 'local', 'output': [], 'usage': {
                        'input_tokens': 12, 'output_tokens': 4}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            server_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            server_thread.start()
            relay = ResponsesRelay(endpoint=f'http://127.0.0.1:{upstream.server_port}', api_key='LOCAL-FAKE',
                budget=budget, config=deepseek.DEEPSEEK_RELAY_CONFIG)
            relay.rows = deepcopy(report['calls'])
            relay.label = ('planning_preflight', 'candidate', -1)
            actor = relay.issue_actor_config('admitted')
            entered, release, close_waiting, phase_done, progressed, revoked = (threading.Event() for _ in range(6))
            lock = relay._upstream_lock
            class ObservedLock:
                def __enter__(self):
                    if threading.current_thread().name == 'phase-finisher':
                        close_waiting.set()
                        progressed.set()
                    lock.acquire()
                def __exit__(self, *_args):
                    lock.release()
            relay._upstream_lock = ObservedLock()
            relay.start()
            outcomes = {}
            original_reserve = budget.reserve
            def paused_reserve(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test did not release admitted reservation')
                return original_reserve(*args, **kwargs)
            def post():
                outcomes['http'] = requests.post(relay.url + '/responses', json={'input': 'local'},
                    headers={'Authorization': 'Bearer ' + actor['api_key']}, timeout=5).status_code
            request_thread = threading.Thread(target=post)
            def execute(**_kwargs):
                request_thread.start()
                if not entered.wait(2):
                    raise AssertionError('request did not enter reserve after second authentication')
                relay.revoke_actor('admitted')
                revoked.set()
                return deepcopy(report['planning_preflight']['row'])
            def finish():
                try:
                    outcomes['phase'] = deepseek.run_planning_preflight(candidate='candidate', relay=relay,
                        output=options['output'], identity=live.apparatus_identity())
                finally:
                    phase_done.set()
                    progressed.set()
            phase_thread = threading.Thread(target=finish, name='phase-finisher')
            try:
                with patch.object(budget, 'reserve', side_effect=paused_reserve), \
                        patch.object(live, '_execute_cell', side_effect=execute):
                    phase_thread.start()
                    self.assertTrue(progressed.wait(2))
                    self.assertTrue(revoked.is_set(), 'actor revocation blocked on upstream I/O')
                    self.assertTrue(close_waiting.is_set(), 'phase returned without draining the admitted request')
                    self.assertFalse(phase_done.is_set())
                    release.set()
                    phase_thread.join(4)
                    request_thread.join(4)
                self.assertFalse(phase_thread.is_alive())
                self.assertFalse(request_thread.is_alive())
                self.assertEqual(outcomes['http'], 200)
                self.assertEqual(outcomes['phase']['status'], 'failed')
                self.assertEqual(len(outcomes['phase']['attempt_ids']), 5)
                self.assertEqual(len(received), 1)
                self.assertEqual(sum(call['arm'] == 'planning_preflight' for call in budget.campaign_calls()), 5)
                self.assertTrue(relay.planning_preflight_closed)
            finally:
                release.set()
                phase_thread.join(4)
                if request_thread.ident is not None:
                    request_thread.join(4)
                relay.close()
                upstream.shutdown()
                upstream.server_close()
                server_thread.join()


class DeepSeekSmokeTests(unittest.TestCase):
    def test_default_thinking_smoke_uses_auto_nonbeta_tools_and_correlated_continuation(self):
        from benchmarks.v3_deepseek import (DEEPSEEK_ENDPOINT, DEEPSEEK_RELAY_CONFIG,
                                            DEEPSEEK_UNLIMITED_PRICING, run_smoke)
        from benchmarks.v3_responses_relay import ResponsesRelay
        from unittest.mock import patch
        received, rejected, continuations = [], [], {}

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                choice = body.get('tool_choice', 'auto')
                error = None
                if isinstance(choice, dict) or choice == 'required':
                    error = 'forced_choice_with_default_thinking'
                elif any(tool.get('strict') is True for tool in body.get('tools', [])):
                    error = 'strict_requires_beta'
                if error:
                    rejected.append(error)
                    status, result = 400, {'error': {'type': 'invalid_request_error', 'message': error}}
                else:
                    received.append((self.path, body))
                    probe = body['tools'][0]['parameters']['properties']['value']['enum'][0]
                    if choice == 'none':
                        expected = continuations[probe] + [{'type': 'function_call_output',
                            'call_id': 'call-' + probe, 'output': json.dumps({'value': probe})},
                            {'role': 'system', 'content': 'Current request tool results are complete.'}]
                        if body['input'] != expected:
                            status, result = 400, {'error': {'type': 'invalid_request_error'}}
                        else:
                            status, result = 200, {'id': 'final-' + probe, 'output': [
                                {'type': 'message', 'role': 'assistant', 'content': [
                                    {'type': 'output_text', 'text': probe}]}]}
                    else:
                        output = [{'type': 'reasoning', 'id': 'reasoning-' + probe, 'summary': [],
                                   'content': [{'type': 'reasoning_text', 'text': 'tool preflight'}]},
                                  {'type': 'function_call', 'name': 'echo_probe', 'call_id': 'call-' + probe,
                                   'arguments': json.dumps({'value': probe})}]
                        continuations[probe] = body['input'] + output
                        status, result = 200, {'id': 'first-' + probe, 'output': output}
                    if status == 200:
                        result.update(model='deepseek-flash', status='completed', usage={
                            'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110})
                payload = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f'http://127.0.0.1:{upstream.server_port}'
            # These controls specify the offline provider's compatibility contract,
            # not the unretained free-form error from the actual paid request.
            for choice, strict in (({'type': 'function', 'name': 'echo_probe'}, False),
                                   ('required', False), ('auto', True)):
                response = requests.post(endpoint + '/responses', json={
                    'tool_choice': choice, 'tools': [{'strict': strict}]}, timeout=3)
                self.assertEqual(response.status_code, 400)
            post = requests.post
            def loopback(url, **kwargs):
                if url == DEEPSEEK_ENDPOINT + '/responses':
                    self.assertEqual(kwargs['headers']['Authorization'], 'Bearer fake')
                    url = endpoint + '/responses'
                self.assertTrue(url.startswith('http://127.0.0.1:'))
                return post(url, **kwargs)
            def request_with_fresh_system(url, **kwargs):
                if kwargs.get('json', {}).get('tool_choice') == 'none':
                    kwargs['json']['input'].append(
                        {'role': 'system', 'content': 'Current request tool results are complete.'})
                return loopback(url, **kwargs)
            with tempfile.TemporaryDirectory() as directory, patch.object(requests, 'post', side_effect=request_with_fresh_system):
                ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb=None,
                                                pricing=DEEPSEEK_UNLIMITED_PRICING)
                relay = ResponsesRelay(endpoint=DEEPSEEK_ENDPOINT, api_key='fake', budget=ledger,
                                       config=DEEPSEEK_RELAY_CONFIG)
                relay.start()
                try:
                    for candidate in ('candidate-one', 'candidate-two'):
                        self.assertTrue(run_smoke(relay, candidate))
                        self.assertTrue(run_smoke(relay, candidate))
                        self.assertEqual(len(relay.smoke_verified_attempt_ids), 2)
                    self.assertIsNone(relay.provider_error)
                    self.assertEqual([row['status'] for row in relay.rows], ['settled'] * 4)
                    self.assertTrue(all(row['usage']['served_model'] == 'deepseek-flash'
                        and row['usage']['raw_usage']['total_tokens'] == 110 for row in relay.rows))
                    self.assertEqual(ledger.spent_rmb, Decimal('0.00112'))
                finally:
                    relay.close()
            self.assertEqual(rejected, ['forced_choice_with_default_thinking'] * 2 + ['strict_requires_beta'])
            self.assertEqual(len(received), 4)
            self.assertEqual(len(continuations), 2)
            for probe in continuations:
                self.assertRegex(probe, r'^[0-9a-f]{32}$')
            for index, (path, body) in enumerate(received):
                self.assertEqual(path, '/responses')
                self.assertEqual(body['tool_choice'], 'auto' if index % 2 == 0 else 'none')
                self.assertIs(body['tools'][0]['strict'], False)
                self.assertEqual(body['model'], 'deepseek-flash')
                self.assertEqual(body['max_output_tokens'], 4096)
                self.assertNotIn('thinking', body)
                self.assertNotIn('reasoning', body)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_smoke_and_retry_use_one_ledger_and_never_release_unknown_usage(self):
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG, run_smoke
        from benchmarks.v3_responses_relay import ResponsesRelay
        received = []
        complete_usage = [False]

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                request = received[-1]
                continuation = request.get('input', [])
                if isinstance(continuation, list) and continuation[-1].get('type') == 'function_call_output':
                    result = {'id': 'smoke-response-2', 'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': json.loads(continuation[-1]['output'])['value']}]}]}
                else:
                    value = request['tools'][0]['parameters']['properties']['value']['enum'][0]
                    result = {'id': 'smoke-response-1', 'output': [{'type': 'function_call',
                        'name': 'echo_probe', 'call_id': 'probe-call-1', 'arguments': json.dumps({'value': value})}]}
                if complete_usage[0]:
                    result['usage'] = {'input_tokens': 100, 'output_tokens': 10}
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            pricing=DEEPSEEK_PRICING)
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                for retry in (False, True):
                    relay = ResponsesRelay(endpoint=f'http://127.0.0.1:{upstream.server_port}',
                                           api_key='fake', budget=ledger, config=DEEPSEEK_RELAY_CONFIG)
                    relay.rows.extend(ledger.campaign_calls())
                    relay.start()
                    try:
                        complete_usage[0] = retry
                        self.assertEqual(run_smoke(relay, 'candidate'), retry)
                        if retry:
                            self.assertTrue(run_smoke(relay, 'candidate'))
                        else:
                            self.assertEqual(relay.provider_error, 'smoke_incomplete_usage')
                            self.assertEqual(ledger.remaining_rmb, Decimal('12.967232'))
                    finally:
                        relay.close()
                self.assertEqual(len(received), 3)
                self.assertEqual(received[-1]['input'][-1]['call_id'], 'probe-call-1')
                self.assertEqual(received[-1]['input'][-1]['type'], 'function_call_output')
                self.assertEqual(received[-1]['input'][-2]['type'], 'function_call')
                self.assertEqual(sorted(row['status'] for row in ledger.campaign_calls()), ['settled', 'settled', 'unknown'])
                self.assertEqual(ledger.remaining_rmb, Decimal('12.966672'))
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()

    def test_malformed_tool_call_arguments_or_final_answer_stop_smoke(self):
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING, DEEPSEEK_RELAY_CONFIG, run_smoke
        from benchmarks.v3_responses_relay import ResponsesRelay
        mode, received = ['bad-name'], []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(body)
                continuation = body['input'][-1].get('type') == 'function_call_output'
                if continuation:
                    output = [{'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': 'wrong final answer'}]}]
                else:
                    value = body['tools'][0]['parameters']['properties']['value']['enum'][0]
                    call = {'type': 'function_call', 'name': 'echo_probe', 'call_id': 'typed-call',
                            'arguments': json.dumps({'value': value})}
                    if mode[0] == 'bad-name':
                        call['name'] = 'another_tool'
                    elif mode[0] == 'no-call-id':
                        call['call_id'] = ''
                    elif mode[0] == 'bad-args':
                        call['arguments'] = '{'
                    elif mode[0] == 'wrong-value':
                        call['arguments'] = json.dumps({'value': 'wrong'})
                    output = [call]
                    if mode[0] == 'no-call':
                        output = []
                    elif mode[0] == 'two-calls':
                        output.append(dict(call, call_id='extra-call'))
                result = {'id': 'response', 'output': output,
                          'usage': {'input_tokens': 100, 'output_tokens': 10}}
                if continuation and mode[0] == 'unknown-final-usage':
                    result.pop('usage')
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            for invalid, call_count, remaining in (
                    ('bad-name', 1, '14.99972'), ('no-call-id', 1, '14.99972'),
                    ('bad-args', 1, '14.99972'), ('wrong-value', 1, '14.99972'),
                    ('no-call', 1, '14.99972'), ('two-calls', 1, '14.99972'),
                    ('bad-final', 2, '14.99944'), ('unknown-final-usage', 2, '12.966952')):
                with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    mode[0] = invalid
                    received.clear()
                    ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                                    pricing=DEEPSEEK_PRICING)
                    relay = ResponsesRelay(endpoint=f'http://127.0.0.1:{upstream.server_port}', api_key='fake',
                                           budget=ledger, config=DEEPSEEK_RELAY_CONFIG)
                    relay.start()
                    try:
                        self.assertFalse(run_smoke(relay, 'candidate'))
                        self.assertTrue(relay.provider_error.startswith('smoke_'))
                        self.assertEqual(len(received), call_count)
                        self.assertEqual(ledger.remaining_rmb, Decimal(remaining))
                        self.assertIsNone(getattr(relay, 'smoke_verified_candidate', None))
                    finally:
                        relay.close()
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()
