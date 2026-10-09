"""Reproductions of verified GitHub review findings for V3 issue #231."""
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
import requests
from openkyrozen.providers.base import get_model_response
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.errors import ProviderError, ProviderErrorKind as K, normalize_provider_error
from openkyrozen.providers.models import ModelResponse
from openkyrozen.providers.retry import provider_call, provider_stream, provider_request_scope, _request, _retry_with_backoff
from tests.test_provider_errors import StatusError, probe, fallback


class GitHubReviewRegressions(unittest.TestCase):
    def test_unread_error_response_does_not_mask_http_status(self):
        error = StatusError(503)
        error.body = None
        error.response = httpx.Response(503, stream=httpx.ByteStream(b'{}'))
        result = normalize_provider_error(error)
        self.assertEqual(result.kind,K.SERVER)
        self.assertIs(result.__cause__,error)

    def test_spent_fallback_budget_is_a_terminal_typed_error(self):
        for streaming in (False,True):
            primary=probe('a')
            primary.chat_stream=Mock(return_value=iter(['unexpected']))
            with self.subTest(streaming=streaming), provider_request_scope(timeout=1) as state:
                state.attempts=4
                with self.assertRaises(ProviderError) as caught:
                    if streaming:
                        list(fallback(primary).chat_stream([]))
                    else:
                        fallback(primary).chat_response([])
                self.assertTrue(caught.exception.terminal)
                self.assertEqual(caught.exception.attempts,4)
                primary.chat.assert_not_called()
                primary.chat_stream.assert_not_called()

    def test_retry_after_that_cannot_fit_returns_classified_error_immediately(self):
        call=Mock(side_effect=StatusError(429,headers={'Retry-After':'120'}))
        started=time.monotonic()
        with provider_request_scope(timeout=0.1), self.assertRaises(ProviderError) as caught:
            _retry_with_backoff(call)
        self.assertEqual(caught.exception.kind,K.RATE_LIMIT)
        self.assertLess(time.monotonic()-started,0.08)
        call.assert_called_once()

    def test_azure_identity_error_shapes_allow_authentication_fallback(self):
        for name,module in [('ClientAuthenticationError','azure.core.exceptions'),
                            ('CredentialUnavailableError','azure.identity')]:
            cls=type(name,(Exception,),{'__module__':module})
            error=cls('private credential error')
            self.assertEqual(normalize_provider_error(error).kind,K.AUTHENTICATION)
            primary,secondary=probe('a',error),probe('b')
            self.assertEqual(fallback(primary,secondary).chat([])[0],'ok')
            primary.chat.assert_called_once()

    def test_requests_truncated_response_is_retryable_transport(self):
        error=requests.exceptions.ChunkedEncodingError('synthetic interruption')
        result=normalize_provider_error(error)
        self.assertEqual(result.kind,K.TRANSPORT)
        self.assertTrue(result.retryable)
        call=Mock(side_effect=[error,'ok'])
        self.assertEqual(_retry_with_backoff(call,base_delay=0),'ok')
        self.assertEqual(call.call_count,2)

    def test_direct_stream_iteration_failure_keeps_attempt_count(self):
        from openkyrozen.providers.openai import OpenAICompatProvider
        def transport():
            yield NS(choices=[NS(delta=NS(content='first'))],usage=None)
            raise httpx.ReadError('synthetic failure')
        provider=OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config=ProviderConfig(provider='custom',model_simple='fixture')
        send=Mock(side_effect=lambda **kwargs:transport())
        provider._client=NS(chat=NS(completions=NS(create=send)))
        stream=provider.chat_stream([])
        self.assertEqual(next(stream),'first')
        with self.assertRaises(ProviderError) as caught:
            next(stream)
        self.assertEqual(caught.exception.attempts,1)
        self.assertTrue(caught.exception.terminal)
        send.assert_called_once()

    def test_standalone_call_deadline_bounds_an_uncooperative_transport(self):
        release,finished=threading.Event(),threading.Event()
        class Adapter:
            name='fixture'
            config=ProviderConfig(provider='custom',model_simple='fixture')
            @provider_call
            def chat_response(self,messages,model=None):
                try:
                    release.wait(0.5)
                    return ModelResponse(text='late')
                finally:
                    finished.set()
        try:
            started=time.monotonic()
            with provider_request_scope(timeout=0.03), self.assertRaises(ProviderError) as caught:
                Adapter().chat_response([])
            self.assertEqual(caught.exception.kind,K.TIMEOUT)
            self.assertLess(time.monotonic()-started,0.25)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))

    def test_paused_stream_does_not_leak_context_into_unrelated_requests(self):
        release=threading.Event()
        states=[]
        class Adapter:
            name='fixture'
            config=ProviderConfig(provider='custom',model_simple='fixture')
            @provider_stream
            def chat_stream(self,messages,model=None):
                states.append(_request.get())
                yield 'first'
                release.wait(0.5)
        stream=Adapter().chat_stream([])
        try:
            self.assertEqual(next(stream),'first')
            self.assertIsNone(_request.get())
            states[0].deadline=time.monotonic()-1
            self.assertEqual(get_model_response(probe('unrelated'),[]).text,'ok')
        finally:
            release.set()
            stream.close()

    def test_standalone_stream_deadline_bounds_stalled_iteration(self):
        release,closed=threading.Event(),threading.Event()
        class Adapter:
            name='fixture'
            config=ProviderConfig(provider='custom',model_simple='fixture')
            @provider_stream
            def chat_stream(self,messages,model=None):
                try:
                    yield 'first'
                    release.wait(0.5)
                    yield 'late'
                finally:
                    closed.set()
        try:
            with provider_request_scope(timeout=0.03):
                stream=Adapter().chat_stream([])
                self.assertEqual(next(stream),'first')
                started=time.monotonic()
                with self.assertRaises(ProviderError):
                    next(stream)
                self.assertLess(time.monotonic()-started,0.25)
        finally:
            release.set()
            stream.close()
        self.assertTrue(closed.wait(1))

    def test_actual_azure_identity_exception_types(self):
        try:
            from azure.core.exceptions import ClientAuthenticationError
            from azure.identity import CredentialUnavailableError
        except ImportError:
            self.skipTest('optional Azure identity SDK unavailable')
        for source in (ClientAuthenticationError('synthetic credential failure'),
                       CredentialUnavailableError('synthetic credential failure')):
            error=normalize_provider_error(source,'azure','deployment')
            self.assertEqual(error.kind,K.AUTHENTICATION)
            self.assertIs(error.__cause__,source)
            self.assertNotIn('synthetic',str(error))

    def test_paused_fallback_stream_does_not_leak_scope(self):
        release=threading.Event()
        primary=probe('a')
        def source(*args):
            yield 'first'
            release.wait(0.5)
        primary.chat_stream=source
        stream=fallback(primary).chat_stream([])
        try:
            self.assertEqual(next(stream),'first')
            self.assertIsNone(_request.get())
            self.assertEqual(get_model_response(probe('unrelated'),[]).text,'ok')
        finally:
            release.set()
            stream.close()

    def test_legacy_context_error_is_typed_at_structured_runtime_boundary(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'KYROZEN_DISABLE_VECTOR_INDEX':'1'}):
            app=build_application(memory=build_memory(Path(directory)/'state.sqlite3'))
            app.runtime.llm_provider=NS(chat=lambda *args:('[Ollama Error] maximum context length exceeded',None))
            try:
                with self.assertRaises(ProviderError) as caught:
                    app.runtime._get_model_response([])
                self.assertEqual(caught.exception.kind,K.CONTEXT_OVERFLOW)
            finally:
                app.close()

    def test_completed_stream_preserves_borrowed_request_scope(self):
        class Adapter:
            name='fixture'
            config=ProviderConfig(provider='custom',model_simple='fixture')
            @provider_stream
            def chat_stream(self,messages,model=None):
                yield 'ok'
        with provider_request_scope(timeout=1) as state:
            self.assertEqual(list(Adapter().chat_stream([])),['ok'])
            self.assertFalse(state.cancelled.is_set())
            self.assertEqual(get_model_response(probe('other'),[]).text,'ok')
