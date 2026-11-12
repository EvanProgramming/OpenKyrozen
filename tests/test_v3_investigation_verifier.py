"""Source-fact grading must not depend on English or assignment formatting."""
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import prepare_fixture, verify_fixture


class InvestigationVerifierTests(unittest.TestCase):
    def verify(self, index, answer, *, read=True, edit=False, plan=None):
        case = next(item for item in FIXTURES if item['fixture_id'] == f'investigation-{index}')
        path = next(iter(case['files']))
        worker = {'provider_error': False, 'processes_alive': 0, 'working_plan': plan,
                  'tool_receipts': [{'action': 'read_file', 'target_path': path,
                                     'authorized': True, 'success': True}] if read else []}
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
                                    plan={'status': 'completed', 'tasks': [{'status': 'succeeded'}, {'status': 'cancelled'}]}))

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
