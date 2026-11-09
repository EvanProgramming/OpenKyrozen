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


def _fixture_response_bytes(response, body, path, *, google):
    """Wire bytes from the acceptance upstream, shared by rootless and kernel tests."""
    streaming=(path.split('?',1)[0].endswith(':streamGenerateContent') if google else bool(body.get('stream')))
    if streaming:
        if google:
            payload=('data: '+json.dumps(response)+'\n\n').encode()
        else:
            events=[{'type':'response.completed','response':response}]
            payload=''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
    else:
        payload=json.dumps(response).encode()
    return ('text/event-stream' if streaming else 'application/json'),payload


class NativeFixtureWireTests(unittest.TestCase):
    def test_responses_fixture_keeps_its_original_event_envelope(self):
        response={'id':'offline','status':'completed'}
        content_type,payload=_fixture_response_bytes(response,{'stream':True},'/v1/responses',google=False)
        self.assertEqual(content_type,'text/event-stream')
        self.assertEqual(payload,('event: response.completed\ndata: '+json.dumps({
            'type':'response.completed','response':response})+'\n\n').encode())
        content_type,payload=_fixture_response_bytes(response,{},'/v1/responses',google=False)
        self.assertEqual((content_type,payload),('application/json',json.dumps(response).encode()))

    def test_real_google_stream_and_tool_continuation_use_native_fixture_bytes(self):
        from tests.test_v3_gemini import NativeSdkGatewayTests,HAS_GOOGLE_SDK
        if not HAS_GOOGLE_SDK:self.skipTest('optional google-genai SDK is not installed')
        from benchmarks.v3_gemini import make_google_provider
        from openkyrozen.providers import ProviderConfig
        from openkyrozen.providers.models import ResponseComplete,ResponseError,ToolReply
        from openkyrozen.tools.catalog import builtin_metadata
        from openkyrozen.agent.engine import _COMPACT_CORE_TOOLS
        fixture=NativeSdkGatewayTests('test_real_sdk_native_function_continuation_and_parent_selected_model')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        model=fixture.policy.role_models['simple'];captured=[]
        tools=[{'name':name,'description':name,'parameters':builtin_metadata(name)['input_schema']}
            for name in sorted(_COMPACT_CORE_TOOLS) if builtin_metadata(name)['capability'] in {'read','network'}]
        def upstream(url,**options):
            body=json.loads(options['data']);captured.append((url,body))
            response=fixture.response(model,[{'functionCall':{'name':'read_file','args':{'path':'source.py'}},
                'thoughtSignature':'b3BhcXVlLXNpZw=='}] if len(captured)==1 else [{'text':'continued'}])
            content_type,payload=_fixture_response_bytes(json.loads(response.content),body,url,google=True)
            response.headers['Content-Type']=content_type;response._content=payload
            return response
        provider=make_google_provider(ProviderConfig(**fixture.config));transport=type(provider._client._api_client._httpx_client)
        original_send=transport.send
        def send(client,request,*args,**kwargs):
            self.assertEqual(request.url.host,'127.0.0.1')
            return original_send(client,request,*args,**kwargs)
        with patch.object(transport,'send',new=send),patch('benchmarks.v3_gemini.requests.post',side_effect=upstream):
            try:
                messages=[{'role':'user','content':'Read assigned source.py'}]
                events=list(provider.chat_response_stream(messages,model=model,tools=tools))
                errors=[str(event.error) for event in events if isinstance(event,ResponseError)]
                self.assertFalse(errors,errors)
                first=next(event.response for event in events if isinstance(event,ResponseComplete))
                self.assertEqual(first.tool_calls[0].name,'read_file')
                final=provider.chat_response(messages+[first,ToolReply(first.tool_calls[0].id,'read_file',
                    {'data':{'content':'kernel actor evidence'}})],model=model,tools=tools)
                self.assertEqual(final.text,'continued')
            finally:provider._client.close()
        self.assertIn(':streamGenerateContent?alt=sse',captured[0][0]);self.assertNotIn('stream',captured[0][1])
        self.assertEqual(captured[1][1]['contents'][1]['parts'][0]['thoughtSignature'],'b3BhcXVlLXNpZw==')
        self.assertEqual(len(fixture.budget.campaign_calls()),2)


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
 if os.environ.get('KYROZEN_BENCHMARK_GOOGLE_POLICY'):
  from v3_actor_worker import benchmark_google_provider,benchmark_provider_config
  from openkyrozen.providers import get_provider
  policy=json.loads(os.environ['KYROZEN_BENCHMARK_GOOGLE_POLICY'])
  runtime._provider_config=benchmark_provider_config(policy,api_key=os.environ['KYROZEN_BENCHMARK_API_KEY'],context_window_tokens=1000000)
  runtime.DEEPSEEK_MODEL_SIMPLE=policy['model_simple'];runtime.DEEPSEEK_MODEL_COMPLEX=policy['model_complex']
  runtime.DEEPSEEK_MODEL=policy['model_simple']
  provider=benchmark_google_provider(runtime._provider_config,get_provider)
  provider.chat([{'role':'user','content':'coordinator established'}],model=runtime.DEEPSEEK_MODEL)
  provider._client.close()
 install_delegated_adapter(runtime,client,source=source)
 coordinator=runtime.delegation()
 run={'run_id':'author1','budget_id':'author1','profile':'researcher','name':'Author','reviewer':'Reviewer',
      'review_agent':{},'reviews':[],'status':'succeeded','usage':{},
      'assignment':{'objective':'Read source.py','context':'Inspect actual source evidence','scope':['source.py']}}
 coordinator.runs['author1']=run
 coordinator.cancelled['author1']=threading.Event()
 author=runtime._invoke_delegated(run,review=False,feedback=None,coordinator=coordinator)
 run['report']=json.loads(author['result']); run['result']=author
 reviewer=runtime._invoke_delegated(run,review=True,feedback=None,coordinator=coordinator)
 run['reviews']=[{'session_id':reviewer['session_id']}]
 detail=coordinator.detail('author1')
 journals=None
 if hasattr(app.store,'list_conversation_entries'):
  scope=run.get('conversation_workspace_id',runtime.interaction_workspace_id())
  journals={role:[entry['kind'] for entry in app.store.list_conversation_entries(
      user_id=runtime.memory_bank.user_id,workspace_id=scope,session_id=value['session_id'])]
      for role,value in [('author',author),('reviewer',reviewer)]}
 Path(result).write_text(json.dumps({'author':author,'reviewer':reviewer,
     'records':runtime._benchmark_child_context_records,'usage':run['usage'],
     'messages':detail['messages'],'review_messages':detail['review_messages'],'journals':journals}))
finally: app.close()
'''


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0 and os.name == 'posix',
                     'disposable privileged Unix actor acceptance required')
class NativeActorAcceptanceTests(unittest.TestCase):
    def test_actual_v2_v3_author_review_tool_continuations_and_usage(self):
        self._actual_v2_v3_author_review(google=False)

    def test_actual_v2_v3_google_sdk_author_review_tool_continuations_and_usage(self):
        self._actual_v2_v3_author_review(google=True)

    def _actual_v2_v3_author_review(self, *, google):
        if google:
            try:
                import google.genai
            except ImportError:
                self.skipTest('optional google-genai SDK is not installed')
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
                visible = json.dumps(body.get('contents') if google else body['input'])
                if google and 'coordinator established' in visible:
                    text = 'coordinator established'
                    review, key, count = False, None, -1
                else:
                    text = None
                review = 'Independently verify this assignment and submitted result.' in visible
                key = bool(body.get('tools')), review
                count = counts.get(key, 0)
                if text is None:
                    counts[key] = count + 1
                report = ({'verdict': 'verified', 'summary': 'Independently read actual source',
                           'findings': [], 'evidence': ['source.py']} if review else
                          {'status': 'completed', 'summary': 'Read actual source', 'findings': [],
                           'evidence': ['source.py'], 'artifacts': [], 'checks': ['read'],
                           'uncertainties': [], 'suggested_followups': []})
                if text is not None:
                    output = [{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]
                elif count == 0 and body.get('tools'):
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
                if google:
                    model=self.path.split('/models/',1)[1].split(':',1)[0]
                    parts=([{'functionCall':{'name':'read_file','args':{'path':'source.py'}},
                              'thoughtSignature':'b3BhcXVlLXNpZw=='}] if output[0]['type']=='function_call' else
                           [{'text':output[0]['content'][0]['text']}])
                    response={'modelVersion':model,'candidates':[{'index':0,'finishReason':'STOP',
                        'content':{'role':'model','parts':parts}}],
                        'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20,
                                         'thoughtsTokenCount':3,'totalTokenCount':123}}
                content_type,payload=_fixture_response_bytes(response,body,self.path,google=google)
                self.send_response(200)
                self.send_header('Content-Type', content_type)
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
                    if google:
                        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing,GeminiRelay
                        from openkyrozen.providers import ProviderConfig
                        native_policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
                            model_simple='gemini-3.5-flash-lite',model_complex='gemini-3.8-flash'),
                            pricing=GeminiPricing(billing_tier='free'))
                        budget=BudgetLedger(fixture/'native-budget.sqlite3',limit_rmb='1',pricing=native_policy.pricing)
                        relay=GeminiRelay(endpoint=f'http://127.0.0.1:{server.server_port}',
                            api_key='offline-upstream-only',budget=budget,policy=native_policy)
                    else:
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
                                revoke_actor=relay.revoke_actor,
                                **({'selected_actor_model':relay.selected_actor_model} if google else {})) as supervisor:
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
                        self.assertEqual(len(observed['records']), len(requests) - start - int(google))
                        self.assertTrue(all(record['outcome'] == 'returned' for record in observed['records']))
                        self.assertEqual({item['review'] for item in observed['records']}, {False, True})
                        self.assertEqual(len({row['actor_id'] for row in relay.rows}), 3 if google else 2)
                        self.assertTrue(all(row['status'] == 'settled' and row['usage']['input_tokens'] == 100
                                            for row in relay.rows))
                        self.assertTrue(observed['usage'])
                        self.assertTrue(observed['messages'])
                        self.assertTrue(observed['review_messages'][observed['reviewer']['session_id']])
                        if arm == 'v3':
                            for kinds in observed['journals'].values():
                                self.assertIn('model.response', kinds)
                                self.assertIn('tool.result', kinds)
                        self.assertEqual(guard.read_text(), 'original')
                        self.assertEqual((workspace / 'source.py').read_text(), 'kernel actor evidence')
                    finally:
                        relay.close()
