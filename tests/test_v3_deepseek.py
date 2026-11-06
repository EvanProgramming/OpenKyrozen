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


class DeepSeekBudgetTests(unittest.TestCase):
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
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING
        from benchmarks.v3_responses_relay import RelayConfig, ResponsesRelay
        with tempfile.TemporaryDirectory() as directory:
            ledger = v3_matched.BudgetLedger(Path(directory) / 'budget.db', limit_rmb='15',
                                            pricing=DEEPSEEK_PRICING)
            for config in (RelayConfig(), RelayConfig(model='deepseek-flash'),
                           RelayConfig(model='deepseek-flash', fixed_input_token_bound=999_999, serial_requests=True)):
                with self.subTest(config=config), self.assertRaises(ValueError):
                    ResponsesRelay(endpoint='http://127.0.0.1:1', api_key='fake', budget=ledger, config=config)

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

        class Relay:
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
            Path(command[command.index('--result') + 1]).write_text('{}')
            return 0, ''

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            from tests.test_v3_matched_live import prepare_candidate_apparatus
            prepare_candidate_apparatus(root / 'src')
            with patch.object(live, '_source_snapshot', return_value='candidate'), \
                    patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}), \
                    patch('benchmarks.v3_deepseek.run_smoke', return_value=True), \
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


class DeepSeekSmokeTests(unittest.TestCase):
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
