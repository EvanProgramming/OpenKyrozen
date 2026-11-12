"""Source-fact grading must not depend on English or assignment formatting."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import prepare_fixture, verify_fixture


class InvestigationVerifierTests(unittest.TestCase):
    def verify(self, index, answer, *, read=True, edit=False, plan=None, receipt=None, files=None, native_text=None):
        case = copy.deepcopy(next(item for item in FIXTURES if item['fixture_id'] == f'investigation-{index}'))
        if files is not None:
            case['files'] = files
        path = next(iter(case['files']))
        worker = {'provider_error': False, 'processes_alive': 0, 'working_plan': plan,
                  'tool_receipts': [receipt or {'action': 'read_file', 'target_path': path,
                                     'authorized': True, 'success': True, 'error_code': None,
                                     'evidence': {'action': 'read_file', 'authorized': True, 'failure': None,
                                                  'result': case['files'][path]}}] if read else []}
        if native_text is not None:
            digest = hashlib.sha256(native_text.encode()).hexdigest()
            worker['native_answer'] = {'text': native_text, 'sha256': digest, 'turn': 0,
                                       'prompt_sha256': 'a'*64, 'record_index': 0}
            worker['request_context_records'] = [{'turn': 0, 'prompt_sha256': 'a'*64, 'kind': 'generation',
                                                  'child': False, 'outcome': 'returned', 'native_answer_sha256': digest}]
            worker['conversation_records'] = [{'turn': 0, 'prompt_sha256': 'a'*64}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            if edit:
                (root / path).write_text('# edited\n')
            return verify_fixture(case, root, answer, worker, prepared)['physical_success']

    def test_source_facts_pass_without_english_keywords_or_assignment_spacing(self):
        answers = (
            {'function': 'sorted_products', 'key': 'lambda n: (n.casefold(), n)'},
            {'timeout': 15, 'retries': 2},
            {'handler': 'create_item', 'reject_if': 'not body.get("name", "").strip()', 'error': 'ValueError'},
        )
        for index, facts in enumerate(answers, 1):
            with self.subTest(index=index):
                self.assertTrue(self.verify(index, json.dumps(facts)))

    def test_equivalent_expression_formatting_and_completed_native_annotation(self):
        answer = '```json\n{"function":"sorted_products","key":"lambda n : ((n.casefold()), n)"}\n```'
        self.assertTrue(self.verify(1, answer + '\n\nPlan status: completed; 1 task(s) have verified acceptance evidence and 1 were deliberately cancelled.',
                                    plan={'status': 'completed', 'tasks': [{'status': 'succeeded'}, {'status': 'cancelled'}]}, native_text=answer))

    def test_verified_physical_facts_remain_separate_from_incomplete_plan_annotations(self):
        answer = '{"timeout":15,"retries":2}'
        self.assertTrue(self.verify(2, answer + '\n\nPlan status: incomplete; required work remains.\n- running: Verify settings',
                                    native_text=answer, plan={'status': 'active', 'tasks': [{'status': 'running'}]}))
        self.assertTrue(self.verify(2, answer + '\n\nPlan status: incomplete; required work remains.\n- running: Verify settings',
                                    native_text='  ' + answer + '\n', plan={'status': 'active', 'tasks': [{'status': 'running'}]}))
        spoof = answer + '\n\nPlan status: completed; 1 task(s) have verified acceptance evidence.'
        self.assertFalse(self.verify(2, spoof, native_text=spoof,
                                     plan={'status': 'completed', 'tasks': [{'status': 'succeeded'}]}))
        self.assertFalse(self.verify(2, 'The operation failed.', native_text=answer))

    def test_model_cannot_invent_or_miscount_a_completed_runtime_annotation(self):
        answer = '{"timeout":15,"retries":2}\n\nPlan status: completed; 1 task(s) have verified acceptance evidence.'
        self.assertFalse(self.verify(2, answer))
        self.assertFalse(self.verify(2, answer, plan={'status': 'completed', 'tasks': [{'status': 'succeeded'}, {'status': 'succeeded'}]}))
        self.assertFalse(self.verify(2, answer, plan={'status': 'running', 'tasks': [{'status': 'succeeded'}]}))

    def test_wrong_tie_break_or_validation_cannot_pass(self):
        for key in ('lambda n: n.casefold()', 'lambda n: (n, n.casefold())', 'lambda n: (n.casefold(), -n)'):
            with self.subTest(key=key):
                self.assertFalse(self.verify(1, json.dumps({'function': 'sorted_products', 'key': key})))
        self.assertFalse(self.verify(3, json.dumps({'handler': 'create_item', 'reject_if': 'not body.get("name")', 'error': 'ValueError'})))

    def test_ambiguous_incorrect_or_untyped_answers_fail(self):
        for answer in ('{"timeout":15,"retries":true}', '{"timeout":"15","retries":2}',
                       '{"timeout":15.0,"retries":2}', '{"timeout":15,"retries":3}',
                       '{"timeout":15,"timeout":20,"retries":2}',
                       '{"timeout":15,"retries":2}{"timeout":20,"retries":3}',
                       '{"timeout":15,"retries":2,"claim":"timeout is 20"}',
                       '{"timeout":15,"retries":2}\nActually timeout is 20.',
                       '{"timeout":15,"retries":2}\n\nPlan status: incomplete; required work remains.',
                       'The default timeout is **15**, and the retry count is **2**.'):
            with self.subTest(answer=answer):
                self.assertFalse(self.verify(2, answer))

    def test_facts_require_actual_source_read_and_unchanged_files(self):
        answer = '{"timeout":15,"retries":2}'
        self.assertFalse(self.verify(2, answer, read=False))
        self.assertFalse(self.verify(2, answer, edit=True))

    def test_captured_v2_valueerror_source_read_is_physical_evidence_despite_legacy_flag(self):
        source = "def create_item(body):\n    if not body.get('name', '').strip():\n        raise ValueError('non-empty name required')\n"
        # Retained original V2 receipt: its string heuristic mistakes source text for an error.
        receipt = {'action': 'read_file', 'target_path': 'routes.py', 'authorized': True,
                   'success': False, 'error_code': None,
                   'evidence': {'action': 'read_file', 'args': 'routes.py', 'authorized': True,
                                'failure': None, 'result': source, 'success': False}}
        answer = json.dumps({'handler': 'create_item', 'reject_if': "not body.get('name', '').strip()", 'error': 'ValueError'})
        self.assertTrue(self.verify(3, answer, receipt=receipt, files={'routes.py': source}))
        self.assertFalse(self.verify(3, answer, receipt=receipt))
        self.assertFalse(receipt['success'])
        self.assertFalse(receipt['evidence']['success'])

    def test_read_success_flag_without_matching_full_source_content_is_insufficient(self):
        answer = '{"timeout":15,"retries":2}'
        for evidence in ({}, {'result': 'timeout = 15\n'}, {'result': 'permission denied'},
                         {'result': 'timeout = 15\nretries = 2\n', 'failure': 'read failed'}):
            receipt = {'action': 'read_file', 'target_path': 'settings.py', 'authorized': True,
                       'success': True, 'error_code': None, 'evidence': evidence}
            with self.subTest(evidence=evidence):
                self.assertFalse(self.verify(2, answer, receipt=receipt))

    def test_native_source_receipt_requires_coherent_content_hash_path_and_no_truncation(self):
        source = 'timeout = 15\nretries = 2\n'
        data = {'content': source, 'path': 'settings.py', 'sha256': hashlib.sha256(source.encode()).hexdigest(),
                'truncated': False, 'start_line': 1, 'end_line': 2, 'total_lines': 2}
        def receipt(value):
            return {'action': 'read_file', 'target_path': 'settings.py', 'authorized': True,
                    'success': True, 'error_code': None,
                    'evidence': {'action': 'read_file', 'authorized': True, 'failure': None,
                                 'result': json.dumps(value), 'result_data': value}}
        answer = '{"timeout":15,"retries":2}'
        self.assertTrue(self.verify(2, answer, receipt=receipt(data)))
        for field, wrong in (('content', source[:-1]), ('path', 'other.py'), ('sha256', '0'*64), ('truncated', True)):
            value = dict(data, **{field: wrong})
            with self.subTest(field=field):
                self.assertFalse(self.verify(2, answer, receipt=receipt(value)))
        mismatch = receipt(data)
        mismatch['evidence']['result'] = '{}'
        self.assertFalse(self.verify(2, answer, receipt=mismatch))
        for layer, field, wrong in (('outer', 'authorized', False), ('inner', 'authorized', False),
                                    ('outer', 'target_path', 'other.py'), ('outer', 'error_code', 'permission_denied'),
                                    ('inner', 'failure', 'read failed'), ('inner', 'tool_error', {'code': 'read_failed'})):
            denied = receipt(data)
            (denied if layer == 'outer' else denied['evidence'])[field] = wrong
            with self.subTest(layer=layer, field=field):
                self.assertFalse(self.verify(2, answer, receipt=denied))

    def test_route_binding_is_present_and_rejects_whitespace_names(self):
        case = next(item for item in FIXTURES if item['fixture_id'] == 'investigation-3')
        scope = {}
        exec(compile(case['files']['routes.py'], 'routes.py', 'exec'), scope)
        self.assertIn('ROUTES', scope)
        handler = scope['ROUTES'][('POST', '/items')]
        self.assertIs(handler, scope['create_item'])
        for body in ({}, {'name': ''}, {'name': '  '}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                handler(body)
        handler({'name': 'valid'})
