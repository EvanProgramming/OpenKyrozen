import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class PlanningServiceOwnershipTests(unittest.TestCase):
    def test_tasks_service_owns_strategic_lifecycle_without_loading_agent(self):
        code = '''
            import importlib.abc
            import json
            import sys
            from pathlib import Path
            class NoAgent(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'openkyrozen.agent' or fullname.startswith('openkyrozen.agent.'):
                        raise AssertionError('Planning feature imported agent implementation: ' + fullname)
            sys.meta_path.insert(0, NoAgent())
            from openkyrozen.tasks.planning import PlanningService, InteractionError
            from openkyrozen.tasks.engine import TaskManager
            from openkyrozen.persistence.store import EventStore
            store = EventStore(Path(sys.argv[1]) / 'state.sqlite3')
            planner = PlanningService(store, user_id='alice', workspace_id='project', session_id='work')
            manager = TaskManager(store, user_id='alice', workspace_id='project', session_id='work')
            proposal = planner.propose_plan({
                'title': 'Inspect', 'summary': 'Inspect exact source',
                'bounds': {'workspace_paths': ['note.txt'], 'capabilities': ['read'],
                           'effects': ['read_only'], 'risk_ceiling': 'normal'},
                'steps': [{'id': 'inspect', 'title': 'Inspect', 'description': 'Read note.txt',
                           'acceptance': [{'criterion': 'Exact source checked', 'verification': {
                               'action': 'read_file', 'arguments': {'path': 'note.txt'}, 'expect': {'equals': 'known'}}}]}],
            })
            accepted, created = planner.accept_plan(manager, plan_id=proposal['plan_id'],
                version=proposal['version'], fingerprint=proposal['fingerprint'])
            assert created and planner.state()['executing_plan']['plan_id'] == accepted['plan_id']
            try:
                planner.complete_plan(accepted, task_manager=manager)
            except InteractionError:
                pass
            else:
                raise AssertionError('Unverified completion was accepted')
            manager.record_evidence(task_id=accepted['working_task_ids'][0], action='read_file',
                args=json.dumps({'path': 'note.txt'}, sort_keys=True, separators=(',', ':')),
                result='known', verification_result='known', success=True,
                verification_status='not_applicable', receipt_id='verified-source')
            assert manager.set_status(0, 'succeeded')
            assert manager.tasks[0]['status'] == 'succeeded', manager.tasks
            planner.reconcile_plan_completion(manager)
            assert planner.state()['executing_plan'] is None
            assert not any(name == 'openkyrozen.agent' or name.startswith('openkyrozen.agent.') for name in sys.modules)
        '''
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'HOME': directory, 'KYROZEN_DB_PATH': str(Path(directory) / 'isolated.sqlite3'),
                   'KYROZEN_DISABLE_VECTOR_INDEX': '1'}
            result = subprocess.run([sys.executable, '-c', textwrap.dedent(code), directory],
                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_interaction_imports_preserve_error_and_validator_identity(self):
        from openkyrozen.tasks.planning import PlanningService, InteractionError, validate_plan_proposal
        from openkyrozen.agent import modes
        self.assertIs(modes.InteractionError, InteractionError)
        self.assertIs(modes.validate_plan_proposal, validate_plan_proposal)
        self.assertTrue(issubclass(modes.InteractionController, PlanningService))
        with tempfile.TemporaryDirectory() as directory:
            from openkyrozen.persistence.store import EventStore
            controller = modes.InteractionController(EventStore(Path(directory) / 'state.sqlite3'), session_id='ui')
            controller.set_mode('ask')
            proposal = controller.propose_plan({'title': 'Inspect', 'summary': 'Inspect source',
                'steps': [{'id': 'inspect', 'title': 'Inspect', 'description': 'Read source',
                           'acceptance': ['Source checked']}]})
            state = controller.state()
            self.assertEqual(state['preference_mode'], 'ask')
            self.assertEqual(state['pending_plan']['fingerprint'], proposal['fingerprint'])
            controller.cancel_plan(proposal['plan_id'], proposal['version'])
            self.assertIsNone(controller.state()['pending_plan'])
