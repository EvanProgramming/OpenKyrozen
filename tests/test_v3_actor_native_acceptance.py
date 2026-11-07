"""Original V2/V3 author and reviewer runtimes behind the real kernel boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


_ROOT = r'''
import json,os,sys,threading
from pathlib import Path
source,apparatus,workspace,result,url=sys.argv[1:6]
sys.path[:0]=[source,apparatus]
from v3_process_owner import connect_actor_client
from v3_actor_worker import install_delegated_adapter,install_mutation_adapter,install_read_adapter
from v3_shell_launcher import install_worker_adapter
from openkyrozen.app.bootstrap import build_application
from openkyrozen.providers import ProviderConfig
client=connect_actor_client()
install_worker_adapter(client=client)
install_mutation_adapter(client); install_read_adapter(client)
app=build_application(surface='cli')
runtime=app.runtime
try:
 runtime._set_workspace_root(workspace)
 runtime._provider_config=ProviderConfig(provider='openai',api_key=os.environ['KYROZEN_BENCHMARK_API_KEY'],
    base_url=url,model_simple='offline-actor',model_complex='offline-actor',context_window_tokens=1000000)
 runtime.DEEPSEEK_MODEL=runtime.DEEPSEEK_MODEL_SIMPLE=runtime.DEEPSEEK_MODEL_COMPLEX='offline-actor'
 install_delegated_adapter(runtime,client,source=source)
 coordinator=runtime.delegation()
 run={'run_id':'author1','budget_id':'author1','profile':'researcher','name':'Author','reviewer':'Reviewer',
      'review_agent':{},'status':'succeeded','usage':{},
      'assignment':{'objective':'Read source.py','context':'Inspect actual source evidence','scope':['source.py']}}
 coordinator.runs['author1']=run
 coordinator.cancelled['author1']=threading.Event()
 author=runtime._invoke_delegated(run,review=False,feedback=None,coordinator=coordinator)
 run['report']=json.loads(author['result']); run['result']=author
 reviewer=runtime._invoke_delegated(run,review=True,feedback=None,coordinator=coordinator)
 Path(result).write_text(json.dumps({'author':author,'reviewer':reviewer,
     'records':runtime._benchmark_child_context_records,'usage':run['usage']}))
finally: app.close()
'''


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0 and os.name == 'posix',
                     'disposable privileged Unix actor acceptance required')
class NativeActorAcceptanceTests(unittest.TestCase):
    def test_actual_v2_v3_author_review_tool_continuations_and_usage(self):
        from benchmarks import v3_actor_supervisor as supervisor_module
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        from benchmarks.v3_matched import BudgetLedger
        from benchmarks.v3_matched_live import BASELINE, _extract_baseline
        from benchmarks.v3_process_owner import process_snapshot
        from benchmarks.v3_responses_relay import RelayConfig, ResponsesRelay
        from benchmarks.v3_shell_launcher import ShellPolicy

        requests, counts = [], {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                visible = json.dumps(body['input'])
                review = 'Independently verify this assignment and submitted result.' in visible
                key = bool(body.get('tools')), review
                count = counts.get(key, 0)
                counts[key] = count + 1
                report = ({'verdict': 'verified', 'summary': 'Independently read actual source',
                           'findings': [], 'evidence': ['source.py']} if review else
                          {'status': 'completed', 'summary': 'Read actual source', 'findings': [],
                           'evidence': ['source.py'], 'artifacts': [], 'checks': ['read'],
                           'uncertainties': [], 'suggested_followups': []})
                if count == 0 and body.get('tools'):
                    output = [{'type': 'function_call', 'id': 'fc-read', 'call_id': 'call-read',
                               'name': 'read_file', 'arguments': json.dumps({'path': 'source.py'})}]
                else:
                    text = ('Action: {"action":"read_file","args":"source.py"}' if count == 0
                            else json.dumps(report))
                    output = [{'type': 'message', 'id': 'message-local', 'role': 'assistant',
                               'status': 'completed', 'content': [{'type': 'output_text',
                               'text': text, 'annotations': []}]}]
                response = {'id': 'response-local', 'object': 'response', 'status': 'completed',
                            'model': 'offline-actor', 'output': output,
                            'usage': {'input_tokens': 100, 'output_tokens': 20}}
                if body.get('stream'):
                    events = [{'type': 'response.completed', 'response': response}]
                    payload = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'
                                      for event in events).encode()
                else:
                    payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir='/var/tmp', prefix='kyrozen-native-actors-') as directory:
            base = Path(directory).resolve()
            base.chmod(0o711)
            baseline = base / 'baseline'
            _extract_baseline(baseline, repository, BASELINE)
            for arm, selected in (('v2', baseline), ('v3', repository)):
                with self.subTest(arm=arm):
                    counts.clear()
                    start = len(requests)
                    fixture = base / arm
                    fixture.mkdir(mode=0o711)
                    workspace = fixture / 'workspace'
                    workspace.mkdir()
                    (workspace / 'source.py').write_text('kernel actor evidence')
                    guard = workspace / 'test_guard.py'
                    guard.write_text('original')
                    result = fixture / 'result.json'
                    budget = BudgetLedger(fixture / 'fake-transport-budget.sqlite3', limit_rmb='1')
                    relay = ResponsesRelay(endpoint=f'http://127.0.0.1:{server.server_port}/v1',
                        api_key='offline-upstream-only', budget=budget, config=RelayConfig(model='offline-actor'))
                    relay.label = (arm, 'kernel-native', 0)
                    relay.start()
                    try:
                        diagnostics = []
                        original_collect = supervisor_module._communicate
                        def collect(*args, **kwargs):
                            output, error = original_collect(*args, **kwargs)
                            diagnostics.append(error)
                            return output, error
                        command = [sys.executable, '-I', '-c', _ROOT, str(selected), str(repository / 'benchmarks'),
                                   str(workspace), str(result), relay.url, '--model', 'offline-actor', '--result', str(result)]
                        policy = ShellPolicy(workspace, {'read', 'network'})
                        with patch.object(supervisor_module, '_communicate', side_effect=collect), ActorSupervisor(command, cwd=workspace, env={
                                'PATH': os.environ['PATH'], 'KYROZEN_DISABLE_VECTOR_INDEX': '1',
                                'KYROZEN_CLI_CAPABILITIES': 'read,network'}, protected_files=[guard],
                                source=selected, policy=policy, provider_config=relay.issue_actor_config,
                                revoke_actor=relay.revoke_actor) as supervisor:
                            process = supervisor.popen(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            output, error = process.communicate(timeout=90)
                            self.assertEqual(process.returncode, 0, error[-6000:] + b''.join(diagnostics)[-12000:])
                            identities = [actor['owner'].uid for actor in supervisor.actors.values()]
                        self.assertEqual(len(identities), 3)
                        self.assertEqual(len(set(identities)), 3)
                        self.assertFalse(any(uid in identities and not state.startswith(('Z', 'X'))
                                             for uid, state in process_snapshot().values()))
                        observed = json.loads(result.read_text())
                        for role in ('author', 'reviewer'):
                            self.assertTrue(any(item['action'] == 'read_file' and item['success']
                                                for item in observed[role]['tool_records']),
                                            (arm, role, observed[role], counts))
                        self.assertEqual(json.loads(observed['author']['result'])['status'], 'completed')
                        self.assertEqual(json.loads(observed['reviewer']['result'])['verdict'], 'verified')
                        self.assertEqual(len(observed['records']), len(requests) - start)
                        self.assertTrue(all(record['outcome'] == 'returned' for record in observed['records']))
                        self.assertEqual({item['review'] for item in observed['records']}, {False, True})
                        self.assertEqual(len({row['actor_id'] for row in relay.rows}), 2)
                        self.assertTrue(all(row['status'] == 'settled' and row['usage']['input_tokens'] == 100
                                            for row in relay.rows))
                        self.assertTrue(observed['usage'])
                        self.assertEqual(guard.read_text(), 'original')
                        self.assertEqual((workspace / 'source.py').read_text(), 'kernel actor evidence')
                    finally:
                        relay.close()
