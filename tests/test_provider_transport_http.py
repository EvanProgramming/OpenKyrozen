"""Real SDK requests to a local fixture server; no paid provider calls."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
from openkyrozen.providers.retry import provider_request_scope


class LocalSDKTransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 503
        self.release = threading.Event()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get('Content-Length','0'))
                # Retain request shape/count only; credentials are never captured.
                body = json.loads(self.rfile.read(size))
                outer.requests.append((self.path, list(body)))
                if outer.status == 'stall':
                    outer.release.wait(2)
                    return
                self.send_response(outer.status)
                self.send_header('Content-Type','application/json')
                self.send_header('x-amzn-errortype','ServiceUnavailableException')
                self.end_headers()
                payload = {'error':{'code':outer.status,'status':'UNAVAILABLE',
                                    'message':'synthetic failure','type':'overloaded_error'},
                           'message':'synthetic failure'}
                try:
                    self.wfile.write(json.dumps(payload).encode())
                except BrokenPipeError:
                    pass
            def log_message(self,*args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def adapter(self, family):
        try:
            if family in {'openai','responses','azure'}:
                import openai
                from openkyrozen.providers.openai import OpenAICompatProvider, OpenAIResponsesProvider
                from openkyrozen.providers.azure import AzureOpenAIProvider
                cls = {'openai':OpenAICompatProvider,'responses':OpenAIResponsesProvider,'azure':AzureOpenAIProvider}[family]
                return cls(ProviderConfig(provider='custom',api_key='fixture',base_url=self.url+'/v1',model_simple='fixture'))
            if family == 'anthropic':
                import anthropic
                from openkyrozen.providers.anthropic import AnthropicProvider
                with patch.dict('os.environ',{'ANTHROPIC_BASE_URL':self.url}):
                    return AnthropicProvider(ProviderConfig(provider='anthropic',api_key='fixture',base_url=self.url,model_simple='fixture'))
            if family == 'perplexity':
                from perplexity import Perplexity
                from openkyrozen.providers.perplexity import PerplexityProvider
                provider = PerplexityProvider(ProviderConfig(provider='perplexity',api_key='fixture',model_simple='fixture'))
                provider._client.close()
                provider._client = Perplexity(api_key='fixture',base_url=self.url,max_retries=0,timeout=90)
                return provider
            if family in {'google','vertex'}:
                from google import genai
                from google.oauth2.credentials import Credentials
                from openkyrozen.providers.google import GoogleProvider, VertexProvider
                cls = GoogleProvider if family=='google' else VertexProvider
                provider = cls.__new__(cls)
                provider.config = ProviderConfig(provider=family,model_simple='fixture')
                provider._supports_retry_options = "retry_options" in genai.types.HttpOptions.model_fields
                kwargs = {'api_key':'fixture'} if family=='google' else {'vertexai':True,'project':'fixture','location':'global','credentials':Credentials(token='fixture')}
                provider._client = genai.Client(**kwargs,http_options={'base_url':self.url,'api_version':'v1'})
                return provider
            if family == 'bedrock':
                import boto3
                from openkyrozen.providers.bedrock import BedrockProvider
                with patch.dict('os.environ',{'AWS_ACCESS_KEY_ID':'fixture','AWS_SECRET_ACCESS_KEY':'fixture',
                                              'AWS_REGION':'us-east-1','AWS_ENDPOINT_URL_BEDROCK_RUNTIME':self.url}):
                    return BedrockProvider(ProviderConfig(provider='bedrock',model_simple='fixture'))
            if family == 'ollama':
                from openkyrozen.providers.ollama import OllamaNativeProvider
                return OllamaNativeProvider(ProviderConfig(provider='ollama',base_url=self.url,model_simple='fixture'))
        except ImportError as exc:
            self.skipTest(f'optional SDK unavailable: {exc.name}')

    def close(self, provider):
        client = getattr(provider,'_client',None)
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def exercise(self, family, streaming=False):
        provider = self.adapter(family)
        try:
            with patch.dict('os.environ',{'AWS_ACCESS_KEY_ID':'fixture','AWS_SECRET_ACCESS_KEY':'fixture',
                                          'AWS_REGION':'us-east-1','AWS_ENDPOINT_URL_BEDROCK_RUNTIME':self.url}), \
                 patch('openkyrozen.providers.retry.ProviderRequest.wait'), \
                 patch('openkyrozen.providers.usage._track_cost') as charge, \
                 self.assertRaises(ProviderError) as caught:
                if streaming:
                    list(provider.chat_stream([{'role':'user','content':'fixture'}]))
                else:
                    provider.chat_response([{'role':'user','content':'fixture'}])
            self.assertEqual(caught.exception.kind,ProviderErrorKind.SERVER)
            self.assertEqual(caught.exception.attempts,4)
            self.assertEqual(len(self.requests),4)
            charge.assert_not_called()
        finally:
            self.close(provider)

    def test_openai(self): self.exercise('openai')
    def test_responses(self): self.exercise('responses')
    def test_azure(self): self.exercise('azure')
    def test_anthropic(self): self.exercise('anthropic')
    def test_google(self): self.exercise('google')
    def test_vertex(self): self.exercise('vertex')
    def test_bedrock(self): self.exercise('bedrock')
    def test_ollama(self): self.exercise('ollama')
    def test_perplexity(self): self.exercise('perplexity')
    def test_openai_stream_start(self): self.exercise('openai',True)
    def test_anthropic_stream_start(self): self.exercise('anthropic',True)
    def test_google_stream_start(self): self.exercise('google',True)
    def test_bedrock_stream_start(self): self.exercise('bedrock',True)

    def test_openai_receives_remaining_transport_timeout(self):
        self.status = 'stall'
        provider = self.adapter('openai')
        try:
            with provider_request_scope(timeout=0.05), self.assertRaises(ProviderError) as caught:
                provider.chat_response([{'role':'user','content':'fixture'}])
            self.assertEqual(caught.exception.kind,ProviderErrorKind.TIMEOUT)
            self.assertEqual(len(self.requests),1)
        finally:
            self.close(provider)
