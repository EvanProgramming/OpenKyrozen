"""R258: run the complete legacy provider runtime, including durable paging."""
import copy
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.tools import ToolAdapters


class LegacyRuntimeAcceptanceTests(unittest.TestCase):
    def test_real_legacy_turn_archives_results_and_retrieves_after_restart_without_scope_leak(self):
        with tempfile.TemporaryDirectory(prefix='kyrozen-legacy-runtime-') as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            first = 'FIRST_UNIQUE_CONTENT\n' + 'retained evidence\n' * 500 + 'FIRST_TAIL_SENTINEL'
            for name, content in (('first.txt', first), ('second.txt', 'SECOND_UNIQUE_CONTENT'),
                                  ('third.txt', 'THIRD_UNIQUE_CONTENT')):
                (workspace / name).write_text(content)
            environment = {'HOME': directory, 'USERPROFILE': directory, 'KYROZEN_DB_PATH': str(root / 'state.sqlite3'),
                'KYROZEN_SKILLS_DIR': str(root / 'skills'), 'KYROZEN_DISABLE_VECTOR_INDEX': '1',
                'KYROZEN_CLI_CAPABILITIES': 'read', 'KYROZEN_APPROVAL_MODE': 'never'}
            with patch.dict(os.environ, environment, clear=True):
                requests = []
                artifact = []
                class Provider(LLMProvider):
                    def chat(self, messages, model=None):
                        requests.append(copy.deepcopy(messages))
                        number = len(requests)
                        if number <= 3:
                            name = ('first.txt', 'second.txt', 'third.txt')[number - 1]
                            return 'Action: ' + json.dumps({'action': 'read_file', 'args': name}), None
                        visible = '\n'.join(item.get('content', '') for item in messages)
                        if number == 4:
                            found = re.search(r'artifact_[0-9a-f]+', visible)
                            if found is None:
                                return 'No archived output reference was provided.', None
                            artifact.append(found.group())
                            arguments = json.dumps({'artifact_id': artifact[0], 'offset': 0, 'limit': 1000})
                            return 'Action: ' + json.dumps({'action': 'read_tool_artifact', 'args': arguments}), None
                        return 'Verified bounded legacy replay.', None

                def application(user):
                    app = build_application(surface='cli', user_id=user,
                        memory=build_memory(root / 'state.sqlite3', user_id=user, index=None),
                        tools=ToolAdapters(workspace))
                    app.runtime._set_workspace_root(str(workspace))
                    return app

                app = application('alice')
                try:
                    runtime = app.runtime
                    config = ProviderConfig(provider='openai', api_key='offline-port-only',
                        model_simple='offline-legacy', model_complex='offline-legacy')
                    runtime._provider_config = config
                    runtime.llm_provider = runtime._default_turn.provider = Provider(config)
                    runtime.dispatch_learning_cycle = lambda **kwargs: None
                    runtime._plugin_runtime_for_surface = lambda *args: SimpleNamespace(
                        turn_start=lambda **kwargs: None, turn_end=lambda **kwargs: None)
                    runtime.set_interaction_mode('agent')
                    answer = runtime.chat(runtime.current_session, 'Inspect saved notes', profile='researcher')
                    self.assertIn('Verified bounded legacy replay.', answer)
                    self.assertEqual(len(requests), 5)
                    before = '\n'.join(item.get('content', '') for item in requests[3])
                    self.assertNotIn('FIRST_UNIQUE_CONTENT', before)
                    self.assertNotIn('SECOND_UNIQUE_CONTENT', before)
                    self.assertIn('THIRD_UNIQUE_CONTENT', before)
                    after = '\n'.join(item.get('content', '') for item in requests[4])
                    self.assertIn('FIRST_UNIQUE_CONTENT', after)
                finally:
                    app.close()

                for user in ('alice', 'bob'):
                    with self.subTest(user=user):
                        restarted = application(user)
                        try:
                            pages, offset = [], 0
                            while True:
                                receipt = restarted.runtime._run_tool('read_tool_artifact', json.dumps({
                                    'artifact_id': artifact[0], 'offset': offset, 'limit': 1000}), return_receipt=True)
                                if user == 'bob':
                                    self.assertFalse(receipt.success)
                                    self.assertNotIn('FIRST_UNIQUE_CONTENT', receipt.result)
                                    break
                                self.assertTrue(receipt.success, receipt.result)
                                page = receipt.tool_result.data
                                pages.append(page['content'])
                                if page['next_offset'] is None:
                                    retained = json.loads(''.join(pages))['results'][0]['output']['text']
                                    self.assertIn(first, retained)
                                    break
                                offset = page['next_offset']
                        finally:
                            restarted.close()
