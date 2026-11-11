import importlib
import importlib.util
import io
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from decimal import Decimal
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


class PanstarAutoTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('benchmarks.v3_panstar'),
                             'Native Panstar Auto accounting is not implemented')
        self.native = importlib.import_module('benchmarks.v3_panstar')
        self.policy = self.native.PanstarPolicy(price_date='2026-11-11')

    def metadata(self, model='gpt-6.1-sol'):
        return self.policy.metadata(model)

    def test_normal_and_long_context_price_the_whole_request_at_the_observed_boundary(self):
        pricing = self.policy.pricing
        row = self.metadata()
        self.assertEqual(pricing.for_metadata(row, input_tokens=272000).cost(272000, 10),
                         Decimal('0.2872848'))
        self.assertEqual(pricing.for_metadata(row, input_tokens=272001).cost(272001, 10),
                         Decimal('0.574545312'))
        self.assertEqual(pricing.for_metadata(row, input_tokens=1000).cost(1000, 10, 1000),
                         Decimal('0.0001056'))
        older = self.metadata('gpt-6-sol')
        self.assertEqual(pricing.for_metadata(older, input_tokens=1000).cost(1000, 10, 1000),
                         Decimal('0.0001584'))

    def test_full_input_reservation_covers_the_more_expensive_cache_write_rate(self):
        for model in self.policy.role_models.values():
            with self.subTest(model=model):
                row = self.metadata(model)
                quote = self.policy.pricing.for_metadata(row, input_tokens=1048576).cost(1048576, 4096)
                self.assertEqual(quote, Decimal('2.80068096'))
                actual = self.policy.pricing.for_metadata(row, input_tokens=1000000)
                self.assertEqual(actual.cost(1000000, 4096, 0, 1000000), Decimal('2.67244032'))
                self.assertGreater(quote, actual.cost(1000000, 4096, 0, 1000000))
        self.assertIsNone(self.policy.pricing.max_limit_cny)

    def test_unpriced_models_dates_tiers_and_short_context_cache_writes_fail_closed(self):
        pricing = self.policy.pricing
        for changed in ({'model': 'gpt-6-astra'}, {'price_date': '2026-11-10'},
                        {'price_date': 'invalid'}, {'billing_tier': 'free'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pricing.for_metadata(dict(self.metadata(), **changed), input_tokens=100)
        with self.assertRaises(ValueError):
            pricing.for_metadata(self.metadata(), input_tokens=100).cost(100, 10, 0, 50)

    def test_auto_has_two_cheap_roles_without_a_pinned_main_model(self):
        self.assertEqual(self.policy.config.provider, 'openai')
        self.assertEqual(self.policy.config.model_main, 'auto')
        self.assertEqual(self.policy.role_models,
                         {'simple': 'gpt-6-sol', 'complex': 'gpt-6.1-sol'})
        with self.assertRaises(ValueError):
            self.policy.check_credential('synthetic-key-before-proof')
        self.assertTrue(self.policy.served_identity('gpt-6.1-sol', 'gpt-6.1-sol'))
        self.assertFalse(self.policy.served_identity('gpt-6.1-sol', 'gpt-6-sol'))

    def test_native_output_already_includes_reasoning_and_rejects_missing_or_inconsistent_usage(self):
        response = {'model': 'gpt-6.1-sol', 'service_tier': 'default', 'usage': {
            'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
            'input_tokens_details': {'cached_tokens': 10},
            'output_tokens_details': {'reasoning_tokens': 5}}}
        usage = self.native.native_usage(response)
        self.assertEqual((usage['output_tokens'], usage['reasoning_tokens']), (20, 5))
        self.assertEqual(usage['cache_read_tokens'], 10)
        for changed in ({'usage': None}, {'service_tier': 'priority'},
                        {'usage': dict(response['usage'], total_tokens=125)},
                        {'usage': dict(response['usage'], output_tokens=True)}):
            with self.subTest(changed=changed):
                self.assertIsNone(self.native.native_usage(dict(response, **changed)))

    def test_malformed_provider_metadata_is_unknown_instead_of_escaping_validation(self):
        raw = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
        for response in ({'usage': raw, 'service_tier': []},
                         {'usage': dict(raw, input_tokens_details=['unpriced'])},
                         {'usage': dict(raw, output_tokens_details='unpriced')}):
            try:
                observed = self.native.native_usage(response)
            except (TypeError, AttributeError):
                self.fail('malformed provider metadata escaped the fail-closed validator')
            self.assertIsNone(observed)

    def test_signed_account_guard_binds_panstar_endpoint_roles_and_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            support = Path(temporary)/'quote.json'
            support.write_text('synthetic-account-quote')
            path = Path(temporary)/'proof.json'
            Path(str(path)+'.asc').write_text('synthetic-signature')
            record = {'schema_version': 1, 'provider': 'openai', 'official_endpoint': self.native.OFFICIAL_ENDPOINT,
                'candidate_sha': 'a'*40, 'model_main': 'auto', 'role_models': self.policy.role_models,
                'billing_tier': 'paid', 'account_billing_status': 'unverified',
                'rate_policy': 'standard_paid_rate_upper', 'conservative_cny_per_usd': '8',
                'credential_sha256': hashlib.sha256(b'synthetic-key').hexdigest(),
                'issued_at': '2026-11-11T00:00:00+00:00', 'expires_at': '2026-11-12T00:00:00+00:00',
                'supporting_evidence': [{'path': str(support), 'bytes': support.stat().st_size,
                                         'sha256': hashlib.sha256(support.read_bytes()).hexdigest()}]}
            now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
            for changed in ({'provider': 'google'}, {'official_endpoint': 'https://unapproved.invalid'},
                            {'role_models': {'simple': 'gpt-6-astra', 'complex': 'gpt-6.1-sol'}}):
                path.write_text(json.dumps(dict(record, **changed), sort_keys=True, separators=(',', ':')))
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    self.policy.verify_billing(path, 'a'*40, verify_signature=lambda *_: True, now=now)
            path.write_text(json.dumps(record, sort_keys=True, separators=(',', ':')))
            self.policy.verify_billing(path, 'a'*40, verify_signature=lambda *_: True, now=now)
            self.policy.check_credential('synthetic-key')
            with self.assertRaises(ValueError):
                self.policy.check_credential('another-key')

    def test_only_declared_text_and_function_requests_are_priced(self):
        valid = {'model': 'gpt-6.1-sol', 'input': 'Read proof.txt', 'max_output_tokens': 999999,
                 'tools': [{'type': 'function', 'name': 'read_file', 'parameters': {'type': 'object'}}]}
        self.assertEqual(self.policy.validate_body(valid)['max_output_tokens'], 4096)
        self.assertEqual(valid['max_output_tokens'], 999999)
        for changed in ({'model': 'gpt-6-astra'}, {'model': 'auto'}, {'service_tier': 'priority'},
                        {'tools': [{'type': 'web_search'}]},
                        {'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'fixture'}]}]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.policy.validate_body(dict(valid, **changed))

    def test_gateway_stops_after_unknown_or_failed_native_response_and_keeps_correct_exposure(self):
        from benchmarks.v3_matched import BudgetLedger
        ordinary = {'id': 'fixture', 'model': 'gpt-6-sol', 'status': 'completed', 'output': [],
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}
        for changes, expected in (({'status': 'in_progress'}, 'unknown'),
                                  ({'model': 'unpriced'}, 'unknown'),
                                  ({'usage': dict(ordinary['usage'], cache_write_tokens=50)}, 'unknown'),
                                  ({'status': 'failed'}, 'settled')):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                budget = BudgetLedger(Path(temporary)/'budget.sqlite3', limit_rmb=None, pricing=self.policy.pricing)
                relay = self.native.PanstarRelay(endpoint='http://127.0.0.1:9/v1', api_key='synthetic-only',
                                                budget=budget, policy=self.policy)
                relay.label = ('v3', 'fixture', 0)
                actor = relay.issue_actor_config('root')
                response = requests.Response(); response.status_code = 200
                response._content = json.dumps(dict(ordinary, **changes)).encode()
                relay.start()
                try:
                    with patch('benchmarks.v3_responses_relay.requests.post', return_value=response) as upstream:
                        for _ in range(2):
                            requests.Session().post(relay.url+'/responses', json={'model': 'gpt-6-sol', 'input': 'fixture'},
                                headers={'Authorization': 'Bearer '+actor['api_key']}, timeout=5)
                        self.assertEqual(upstream.call_count, 1)
                    self.assertIsNotNone(relay.provider_error)
                    calls = budget.campaign_calls()
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]['status'], expected)
                    if expected == 'unknown':
                        self.assertEqual(calls[0]['reserved_rmb'], '2.80068096')
                        self.assertIsNone(calls[0]['actual_rmb'])
                finally:
                    relay.close()

    def test_stream_eof_without_terminal_usage_stops_the_next_request(self):
        from benchmarks.v3_matched import BudgetLedger
        with tempfile.TemporaryDirectory() as temporary:
            budget = BudgetLedger(Path(temporary)/'budget.sqlite3', limit_rmb=None, pricing=self.policy.pricing)
            relay = self.native.PanstarRelay(endpoint='http://127.0.0.1:9/v1', api_key='synthetic-only',
                                            budget=budget, policy=self.policy)
            relay.label = ('v3', 'fixture', 0)
            actor = relay.issue_actor_config('root')
            def incomplete_stream(*_args, **_kwargs):
                response = requests.Response(); response.status_code = 200
                response.headers['Content-Type'] = 'text/event-stream'
                response.raw = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
                return response
            relay.start()
            try:
                with patch('benchmarks.v3_responses_relay.requests.post', side_effect=incomplete_stream) as upstream:
                    with requests.Session() as client:
                        for _ in range(2):
                            client.post(relay.url+'/responses', json={'model': 'gpt-6-sol', 'input': 'fixture',
                                'stream': True}, headers={'Authorization': 'Bearer '+actor['api_key']}, timeout=5)
                    self.assertEqual(upstream.call_count, 1)
                self.assertIsNotNone(relay.provider_error)
                calls = budget.campaign_calls()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]['status'], 'unknown')
                self.assertEqual(calls[0]['reserved_rmb'], '2.80068096')
            finally:
                relay.close()

    def test_root_responses_roles_come_from_the_exact_parent_policy_and_cannot_include_a_key(self):
        from benchmarks import v3_actor_worker as worker
        self.assertTrue(hasattr(worker, 'benchmark_root_provider_config'),
                        'Responses Auto root policy is not bound to the trusted parent')
        policy = {'provider': 'openai', 'model_main': 'auto', 'model_simple': 'gpt-6-sol',
                  'model_complex': 'gpt-6.1-sol', 'base_url': 'http://127.0.0.1:1234/v1'}
        for changed in ({}, {'provider': 'google'}, {'model_main': 'pinned'},
                        {'base_url': 'https://unapproved.invalid'}, {'api_key': 'forged'},
                        {'model_complex': 'gpt-6-sol'}):
            with patch.dict(os.environ, {'KYROZEN_BENCHMARK_RESPONSES_POLICY':
                                         json.dumps(dict(policy, **changed))}, clear=True):
                if changed:
                    with self.subTest(changed=changed), self.assertRaises(PermissionError):
                        worker.benchmark_root_provider_config('responses', 'auto', policy['base_url'],
                                                              'parent-issued-alias', 32000)
                else:
                    config = worker.benchmark_root_provider_config('responses', 'auto', policy['base_url'],
                                                                  'parent-issued-alias', 32000)
                    self.assertEqual((config.model_main, config.model_simple, config.model_complex),
                                     ('auto', 'gpt-6-sol', 'gpt-6.1-sol'))
                    self.assertEqual(config.api_key, 'parent-issued-alias')

    def test_core_admits_panstar_but_protection_still_precedes_requests_and_credentials(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_responses_relay import RelayConfig
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(live, '_source_snapshot', return_value='a'*40), \
                patch('openkyrozen.tools.sandbox.sandbox_command',
                      return_value=SimpleNamespace(backend='fixture', close=lambda: None)), \
                patch('benchmarks.v3_protected_worker.verify_worker_protection',
                      side_effect=RuntimeError('protection sentinel')) as protection, \
                patch.object(self.policy, 'credential') as credential:
            budget = Path(temporary)/'budget.sqlite3'
            with self.assertRaisesRegex(RuntimeError, 'protection sentinel'):
                live.run(source=source, repo=source, output=Path(temporary)/'report.json',
                    budget_path=budget, limit_cny='unlimited', prior_hold_cny='95.870513200007',
                    pricing=self.policy.pricing, relay_config=RelayConfig('auto', 4096, 1048576, True),
                    provider_policy=self.policy, planning_preflight_only=True)
            protection.assert_called_once()
            credential.assert_not_called()
            # The exclusive cohort lease may create SQLite before the OS probe, but admits no attempt.
            if budget.exists():
                with sqlite3.connect(budget) as db:
                    present = db.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone()
                    self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0] if present else 0, 0)

    def test_routing_and_physical_model_exposure_use_the_panstar_policy_without_weakening_joins(self):
        from dataclasses import asdict
        from benchmarks.v3_routing_supplement import validate_observations, intervene, model_exposure
        from openkyrozen.routing.router import RoutingDecision
        model = self.policy.role_models['complex']
        before = RoutingDecision('openai', model, 'standard', None, 'coder', True)
        row = {'before': asdict(before), 'after': asdict(intervene(before, 'planning-never',
               profile_override='auto', interaction_mode='agent')), 'variant': 'planning-never',
               'profile_override': 'auto', 'interaction_mode': 'agent', 'system_one_backend': 'off',
               'decision_assist_backend': 'off', 'requests': [{'model': model, 'reserve_tokens': 4096,
                   'message_projection_sha256': 'a'*64}]}
        self.assertTrue(validate_observations([row], 'planning-never', self.policy))
        request = {'kind': 'generation', 'model': model, 'message_projection_sha256': 'a'*64,
                   'schema_sha256': 'b'*64, 'window_tokens': 32000, 'reserve_tokens': 4096}
        routing = [{'child': False, 'before': {'model': model}, 'after': {'model': model},
                    'model_control_applied': False, 'requests': [request]}]
        ports = [dict(request, outcome='returned', provider_input_tokens=100, provider_output_tokens=20)]
        call = {'attempt_id': 'physical', 'model': model, 'actor_id': 'root', 'actor_role': 'root',
                'root_actor_id': 'root', 'admission_sequence': 0, 'status': 'settled',
                'usage': {'input_tokens': 100, 'output_tokens': 20, 'served_model': model}}
        self.assertTrue(model_exposure(routing, ports, [call], self.policy)['complete'])
        self.assertFalse(model_exposure(routing, ports, [call, call], self.policy)['complete'])
        call['usage']['served_model'] = 'gpt-6-astra'
        self.assertFalse(model_exposure(routing, ports, [call], self.policy)['complete'])

    def test_closed_prior_reconstruction_retains_panstar_unknowns_and_deduplicates_copies(self):
        from benchmarks.v3_matched import BudgetLedger
        from benchmarks.v3_deepseek import read_prior_exposure
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'budget.sqlite3'
            ledger = BudgetLedger(path, limit_rmb=None, pricing=self.policy.pricing)
            metadata = dict(self.metadata(), max_output_tokens=4096, protocol='openai-responses',
                            reserved_rmb='2.80068096')
            raw = {'model': 'gpt-6.1-sol', 'status': 'completed', 'service_tier': 'default', 'usage': {
                'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                'input_tokens_details': {'cached_tokens': 10},
                'output_tokens_details': {'reasoning_tokens': 5}}}
            usage = self.policy.response_usage('gpt-6.1-sol', raw)
            for name, result in (('settled', usage), ('unknown', None)):
                self.assertTrue(ledger.reserve(name, input_token_bound=1048576,
                                              output_token_bound=4096, metadata=metadata))
                ledger.settle(name, result)
            copies = []
            for i in range(2):
                p = Path(temporary)/f'closed-{i}.sqlite3'
                p.write_bytes(path.read_bytes())
                copies.append(p)
            exposure = read_prior_exposure(copies)
            self.assertEqual(exposure.attempt_count, 2)
            self.assertEqual(exposure.held_cny, Decimal('2.80068096'))
            self.assertEqual(exposure.settled_cny, Decimal('0.000201168'))

    def test_real_loopback_keeps_each_selected_model_and_reserves_before_transport(self):
        from benchmarks.v3_matched import BudgetLedger
        captured = []
        ledger = None

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with sqlite3.connect(ledger.path) as db:
                    statuses = [row[0] for row in db.execute('SELECT status FROM attempts')]
                captured.append((body, statuses))
                payload = json.dumps({'id': 'fixture', 'model': body['model'], 'status': 'completed',
                    'service_tier': 'default', 'output': [], 'usage': {
                        'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                        'input_tokens_details': {'cached_tokens': 10},
                        'output_tokens_details': {'reasoning_tokens': 5}}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        relay = None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                ledger = BudgetLedger(Path(temporary)/'budget.sqlite3', limit_rmb=None,
                                      pricing=self.policy.pricing)
                relay = self.native.PanstarRelay(endpoint=f'http://127.0.0.1:{server.server_port}/v1',
                    api_key='synthetic-only', budget=ledger, policy=self.policy)
                relay.label = ('v3', 'fixture', 0)
                relay.root_actor_id_by_label[relay.label] = 'root'
                config = relay.issue_actor_config('root')
                self.assertEqual(config['model_main'], 'auto')
                relay.start()
                for model in self.policy.role_models.values():
                    response = requests.post(relay.url+'/responses', json={'model': model, 'input': 'fixture'},
                        headers={'Authorization': 'Bearer '+config['api_key']}, timeout=5)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(relay.selected_actor_model('root'), model)
                self.assertEqual([row[0]['model'] for row in captured], list(self.policy.role_models.values()))
                self.assertTrue(all('reserved' in row[1] for row in captured))
                self.assertTrue(all(call['status']=='settled' and call['actor_role']=='root'
                                    and call['usage']['served_model']==call['model']
                                    for call in relay.rows))
                self.assertTrue(all(self.policy.accepts_call(call) for call in ledger.campaign_calls()))
        finally:
            if relay is not None:
                relay.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_responses_sdk_smoke_requires_both_roles_and_all_four_settled_calls(self):
        self.assertTrue(hasattr(self.native, 'run_smoke'), 'Both Auto role slots need their own native smoke')
        from benchmarks.v3_matched import BudgetLedger
        captured = []

        def upstream(_url, **options):
            body = json.loads(options['data'])
            captured.append(body)
            if len(captured) % 2:
                output = [{'type': 'function_call', 'id': 'fc-fixture', 'call_id': 'call-fixture',
                           'name': 'benchmark_echo', 'arguments': '{"value":"ready"}'}]
            else:
                self.assertTrue(any(item.get('type') == 'function_call_output' and
                    item.get('call_id') == 'call-fixture' for item in body['input']))
                output = [{'type': 'message', 'id': 'msg', 'role': 'assistant', 'status': 'completed',
                           'content': [{'type': 'output_text', 'text': 'ready', 'annotations': []}]}]
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'id': 'response', 'object': 'response', 'status': 'completed',
                'model': body['model'], 'service_tier': 'default', 'output': output,
                'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}).encode()
            return response

        with tempfile.TemporaryDirectory() as temporary:
            budget = BudgetLedger(Path(temporary)/'budget.sqlite3', limit_rmb=None, pricing=self.policy.pricing)
            relay = self.native.PanstarRelay(endpoint='http://127.0.0.1:9/v1', api_key='synthetic-only',
                                            budget=budget, policy=self.policy)
            relay.start()
            try:
                with patch('benchmarks.v3_responses_relay.requests.post', side_effect=upstream):
                    self.assertTrue(self.native.run_smoke(relay, 'a'*40), relay.provider_error)
                    self.assertTrue(self.native.run_smoke(relay, 'a'*40))
                self.assertEqual([body['model'] for body in captured],
                                 ['gpt-6-sol', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6.1-sol'])
                self.assertTrue(all('repeat the returned value exactly' in body['input'][0]['content']
                                    for body in captured), 'The prompt must request the final answer that smoke verifies')
                self.assertEqual(len(budget.campaign_calls()), 4)
                self.assertTrue(all(row['status'] == 'settled' for row in relay.rows))
                self.assertEqual(len(relay.smoke_verified_attempt_ids), 4)
            finally:
                relay.close()

    def test_completed_tool_acknowledgement_reports_the_continuation_contract_failure(self):
        from benchmarks.v3_matched import BudgetLedger
        captured = []
        def upstream(_url, **options):
            body = json.loads(options['data']); captured.append(body)
            output = ([{'type': 'function_call', 'id': 'fc-fixture', 'call_id': 'call-fixture',
                        'name': 'benchmark_echo', 'arguments': '{"value":"ready"}'}] if len(captured) == 1 else
                      [{'type': 'message', 'id': 'msg', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'Done.', 'annotations': []}]}])
            response = requests.Response(); response.status_code = 200
            response._content = json.dumps({'id': 'fixture', 'status': 'completed', 'model': body['model'],
                'output': output, 'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}).encode()
            return response
        with tempfile.TemporaryDirectory() as temporary:
            budget = BudgetLedger(Path(temporary)/'budget.sqlite3', limit_rmb=None, pricing=self.policy.pricing)
            relay = self.native.PanstarRelay(endpoint='http://127.0.0.1:9/v1', api_key='synthetic-only',
                                            budget=budget, policy=self.policy)
            relay.start()
            try:
                with patch('benchmarks.v3_responses_relay.requests.post', side_effect=upstream):
                    self.assertFalse(self.native.run_smoke(relay, 'a'*40))
                self.assertEqual(relay.provider_error, 'native_smoke_continuation_contract')
                self.assertEqual(len(captured), 2)
                self.assertTrue(all(call['status'] == 'settled' for call in budget.campaign_calls()))
                self.assertFalse(hasattr(relay, 'smoke_verified_candidate'))
            finally:
                relay.close()

    def test_cli_all_three_phases_require_complete_original_success_predicates(self):
        self.assertTrue(hasattr(self.native, 'main'), 'Panstar must use the existing core and continuation runners')
        for phase in ('180', '54', '36'):
            for status, passed, code in (('blocked_provider_error', True, 1), ('incomplete', True, 1),
                                         ('complete', False, 1), ('complete', True, 0)):
                args = ['panstar', '--source', 'source', '--repo', 'repo', '--output', 'out', '--budget-db', 'db',
                        '--candidate-sha', 'a'*40, '--billing-tier', 'paid', '--limit-cny', 'unlimited', '--execute']
                args += ['--prior-ledger', 'prior'] if phase == '180' else ['--core-output', 'core', '--core-budget', 'core.db']
                if phase == '36':
                    args += ['--model-routing']
                report = {'status': status, 'summary': {
                    'release_comparison_passed' if phase == '180' else 'benefit_passed': passed}}
                with self.subTest(phase=phase, status=status, passed=passed), patch('sys.argv', args), \
                        patch('benchmarks.v3_matched_live._source_snapshot', return_value='a'*40), \
                        patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                        patch('benchmarks.v3_deepseek.read_prior_exposure', return_value=SimpleNamespace(
                            total_cny=Decimal('95.870513200007'), records=('immutable-prior',))), \
                        patch('benchmarks.v3_matched_live.run', return_value=report) as core, \
                        patch('benchmarks.v3_routing_supplement.run', return_value=report) as child, \
                        patch('builtins.print'), self.assertRaises(SystemExit) as stopped:
                    self.native.main()
                self.assertEqual(stopped.exception.code, code)
                options = (core if phase == '180' else child).call_args.kwargs
                self.assertEqual(options['provider_policy'].config.model_main, 'auto')
                self.assertIsNone(options['provider_policy'].pricing.max_limit_cny)
                if phase == '180':
                    self.assertEqual(options['prior_hold_cny'], '95.870513200007')
                    self.assertEqual(options['prior_hold_records'], ('immutable-prior',))
                else:
                    self.assertEqual(options['model_routing'], phase == '36')


if __name__ == '__main__':
    unittest.main()
