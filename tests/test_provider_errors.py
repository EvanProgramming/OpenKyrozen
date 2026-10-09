"""Offline regressions for bounded provider transport failures."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from openkyrozen.providers.retry import _retry_with_backoff
from openkyrozen.providers.fallback import FallbackProvider
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.errors import ProviderError


class StatusError(Exception):
    def __init__(self, status, code=None, message="private request sk-secret", headers=None):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": code, "message": message}}
        self.response = NS(headers=headers or {})


def probe(name, error=None, text="ok"):
    return NS(name=name, config=ProviderConfig(provider=name, model_simple=name + "-simple", model_complex=name + "-complex"),
              chat=Mock(side_effect=error, return_value=(text, None)))


def fallback(*providers):
    wrapper = FallbackProvider.__new__(FallbackProvider)
    wrapper._primary, wrapper._fallbacks = providers[0], list(providers[1:])
    return wrapper


class ExistingBoundaryRegressions(unittest.TestCase):
    def test_message_numbers_do_not_trigger_retry(self):
        call = Mock(side_effect=ValueError("Invalid argument containing 500 or 429"))
        with patch("openkyrozen.providers.retry.ProviderRequest.wait"), self.assertRaises(ProviderError):
            _retry_with_backoff(call)
        self.assertEqual(call.call_count, 1)

    def test_structured_status_is_classified_without_message_matching(self):
        call = Mock(side_effect=[StatusError(503, "unavailable", "private"), "ok"])
        with patch("openkyrozen.providers.retry.ProviderRequest.wait"):
            self.assertEqual(_retry_with_backoff(call), "ok")
        self.assertEqual(call.call_count, 2)

    def test_stream_failure_after_output_never_replays(self):
        primary = probe("primary")
        def stream(*args):
            yield "first"
            raise StatusError(503)
        primary.chat_stream = stream
        secondary = probe("secondary")
        secondary.chat_stream = Mock(return_value=iter(["replayed"]))
        result = fallback(primary, secondary).chat_stream([])
        self.assertEqual(next(result), "first")
        with self.assertRaises(ProviderError):
            next(result)
        secondary.chat_stream.assert_not_called()

    def test_unknown_failure_is_terminal(self):
        primary, secondary = probe("primary", ValueError("bad local input")), probe("secondary")
        with self.assertRaises(ProviderError):
            fallback(primary, secondary).chat([])
        secondary.chat.assert_not_called()


class TypedFailureTests(unittest.TestCase):
    def test_categories_safe_details_and_causes(self):
        from openkyrozen.providers.errors import ProviderErrorKind as K, normalize_provider_error
        cases = [(StatusError(429, "rate_limit"), K.RATE_LIMIT),
                 (StatusError(503, "unavailable"), K.SERVER),
                 (StatusError(401, "invalid_api_key"), K.AUTHENTICATION),
                 (StatusError(400, "context_length_exceeded"), K.CONTEXT_OVERFLOW),
                 (StatusError(400, "invalid_request_error"), K.INVALID_REQUEST),
                 (TimeoutError("secret"), K.TIMEOUT),
                 (ConnectionError("secret"), K.TRANSPORT),
                 (ValueError("500"), K.UNKNOWN)]
        for source, kind in cases:
            with self.subTest(kind=kind):
                error = normalize_provider_error(source, "custom", "model")
                self.assertEqual(error.kind, kind)
                self.assertEqual((error.provider, error.model), ("custom", "model"))
                self.assertNotIn("sk-secret", str(error))
                self.assertNotIn("private", str(error))
                self.assertIs(error.__cause__, source)
                self.assertEqual(error.retryable, kind in {K.RATE_LIMIT, K.SERVER, K.TIMEOUT, K.TRANSPORT})

    def test_deadline_and_cancel_prevent_attempts(self):
        from openkyrozen.providers.retry import provider_request_scope
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind as K
        cancelled = threading.Event()
        call = Mock(return_value="late")
        with provider_request_scope(timeout=1, cancelled=cancelled):
            cancelled.set()
            with self.assertRaises(ProviderError) as caught:
                _retry_with_backoff(call)
        self.assertEqual(caught.exception.kind, K.CANCELLED)
        call.assert_not_called()

    def test_nested_retry_and_fallback_share_four_attempts(self):
        calls = []
        def provider(name):
            def fail(*args):
                def transport():
                    calls.append(name)
                    raise StatusError(503)
                return _retry_with_backoff(transport, base_delay=0)
            return NS(name=name, config=ProviderConfig(provider=name, model_simple=name), chat=fail)
        with patch("openkyrozen.providers.retry.ProviderRequest.wait"), self.assertRaises(ProviderError):
            fallback(provider("a"), provider("b")).chat([])
        self.assertEqual(calls, ["a", "b", "a", "b"])


class SDKShapeTests(unittest.TestCase):
    def test_actual_sdk_error_shapes(self):
        try:
            import httpx
            import requests
            from openai import RateLimitError, APITimeoutError, AuthenticationError
            from anthropic import BadRequestError
            from google.genai.errors import ClientError, ServerError
            from botocore.exceptions import ClientError as BedrockError, NoCredentialsError
            from perplexity import APIStatusError
        except ImportError as exc:
            self.skipTest(f"optional SDK unavailable: {exc.name}")
        from openkyrozen.providers.errors import normalize_provider_error, ProviderErrorKind as K
        response = httpx.Response(400, request=httpx.Request("POST", "http://localhost/fixture"))
        ollama = requests.Response()
        ollama.status_code = 400
        ollama._content = b'{"error":"context length exceeded"}'
        cases = [
            (RateLimitError("secret", response=httpx.Response(429, request=response.request), body={"code":"rate_limit"}), K.RATE_LIMIT),
            (AuthenticationError("secret", response=httpx.Response(401, request=response.request), body={}), K.AUTHENTICATION),
            (APITimeoutError(request=response.request), K.TIMEOUT),
            (BadRequestError("secret", response=response, body={"type":"invalid_request_error", "message":"prompt is too long"}), K.CONTEXT_OVERFLOW),
            (ClientError(400, {"error":{"code":400,"status":"INVALID_ARGUMENT","message":"input token count exceeds the maximum number of tokens allowed"}}), K.CONTEXT_OVERFLOW),
            (ServerError(503, {"error":{"code":503,"status":"UNAVAILABLE"}}), K.SERVER),
            (ClientError(499, {"error":{"code":499,"status":"CANCELLED"}}), K.CANCELLED),
            (BedrockError({"Error":{"Code":"ThrottlingException"},"ResponseMetadata":{"HTTPStatusCode":429}}, "Converse"), K.RATE_LIMIT),
            (NoCredentialsError(), K.AUTHENTICATION),
            (APIStatusError("secret", response=httpx.Response(503, request=response.request), body={}), K.SERVER),
            (requests.HTTPError("secret", response=ollama), K.CONTEXT_OVERFLOW),
            (httpx.ReadTimeout("secret"), K.TIMEOUT),
        ]
        for source, kind in cases:
            with self.subTest(sdk=type(source).__module__, kind=kind):
                error = normalize_provider_error(source, "p", "m")
                self.assertEqual(error.kind, kind)
                self.assertNotIn("secret", str(error))
                self.assertIs(error.__cause__, source)

    def test_request_scope_rejects_nonfinite_or_nonpositive_deadlines(self):
        from openkyrozen.providers.retry import provider_request_scope
        for timeout in (float('nan'), float('inf'), 0, -1):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                with provider_request_scope(timeout=timeout):
                    pass


class RuntimeLifecycleTests(unittest.TestCase):
    def runtime(self, timeout=0.1, child_event=None):
        context = NS(child_run_id="child" if child_event is not None else None,
                     coordinator=NS(cancelled={"child":child_event}) if child_event is not None else None)
        return NS(execution_context=context, _provider_timeout_seconds=lambda: timeout)

    def test_timeout_stops_retry_after_uncooperative_callback_returns(self):
        import time
        from openkyrozen.providers.calls import _bounded_provider_call
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind as K
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        attempts = []
        def transport():
            attempts.append(1)
            entered.set()
            release.wait(1)
            raise StatusError(503)
        def callback():
            try:
                return _retry_with_backoff(transport)
            finally:
                finished.set()
        try:
            started = time.monotonic()
            with self.assertRaises(ProviderError) as caught:
                _bounded_provider_call(self.runtime(0.03), callback)
            self.assertEqual(caught.exception.kind, K.TIMEOUT)
            self.assertFalse(caught.exception.retryable)
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(entered.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(attempts, [1])

    def test_child_cancellation_interrupts_backoff_without_cancelling_successful_child(self):
        from openkyrozen.providers.calls import _bounded_provider_call
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind as K
        event, entered = threading.Event(), threading.Event()
        call = Mock(side_effect=StatusError(429, headers={"Retry-After":"0.5"}))
        from openkyrozen.providers.retry import ProviderRequest
        original_wait = ProviderRequest.wait
        def callback():
            return _retry_with_backoff(call, base_delay=0.01)
        def observed_wait(request, seconds):
            entered.set()
            return original_wait(request, seconds)
        timer = threading.Thread(target=lambda: (entered.wait(1), event.set()))
        timer.start()
        with patch.object(ProviderRequest, 'wait', observed_wait), self.assertRaises(ProviderError) as caught:
            _bounded_provider_call(self.runtime(1, event), callback)
        timer.join(1)
        self.assertEqual(caught.exception.kind, K.CANCELLED)
        self.assertEqual(call.call_count, 1)
        fresh = threading.Event()
        self.assertEqual(_bounded_provider_call(self.runtime(1, fresh), lambda: "ok"), "ok")
        self.assertFalse(fresh.is_set())

    def test_stalled_stream_discards_late_chunks_and_closes(self):
        from openkyrozen.providers.calls import _bounded_provider_stream
        from openkyrozen.providers.errors import ProviderError
        release, closed = threading.Event(), threading.Event()
        def stream(*args):
            try:
                yield "first"
                release.wait(1)
                yield "late"
            finally:
                closed.set()
        chunks = []
        try:
            with self.assertRaises(ProviderError):
                _bounded_provider_stream(self.runtime(0.03), NS(chat_stream=stream), [], "m", chunks.append)
        finally:
            release.set()
        self.assertTrue(closed.wait(1))
        self.assertEqual(chunks, ["first"])

    def test_timeout_configuration_and_typed_context_compatibility(self):
        from openkyrozen.providers.calls import _provider_timeout_seconds
        from openkyrozen.agent.context import _is_context_overflow_error
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind as K
        runtime = self.runtime()
        for raw, expected in (("nan",90), ("inf",90), ("invalid",90), ("0",1), ("900",600), ("2.5",2.5)):
            with self.subTest(raw=raw), patch.dict('os.environ', {'KYROZEN_PROVIDER_TIMEOUT_SECONDS':raw}):
                self.assertEqual(_provider_timeout_seconds(runtime), expected)
        self.assertTrue(_is_context_overflow_error(runtime, ProviderError(K.CONTEXT_OVERFLOW)))
        self.assertFalse(_is_context_overflow_error(runtime, ProviderError(K.INVALID_REQUEST)))

    def test_keyboard_interrupt_cancels_worker_before_any_retry(self):
        from openkyrozen.providers.calls import _bounded_provider_call
        from openkyrozen.providers.retry import _request
        captured = []
        entered = threading.Event()
        def callback():
            captured.append(_request.get())
            entered.set()
            return "ok"
        def interrupt(*args, **kwargs):
            self.assertTrue(entered.wait(1))
            raise KeyboardInterrupt
        with patch('openkyrozen.providers.retry.queue.Queue.get', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            _bounded_provider_call(self.runtime(1), callback)
        self.assertTrue(captured[0].cancelled.is_set())


class RetryPolicyTests(unittest.TestCase):
    def test_authentication_uses_next_provider_once_and_terminal_errors_stop(self):
        from openkyrozen.providers.errors import ProviderError
        for status, code in ((400,'context_length_exceeded'),(400,'invalid_request_error')):
            primary, secondary = probe('a',StatusError(status,code)), probe('b')
            with self.assertRaises(ProviderError):
                fallback(primary, secondary).chat([])
            secondary.chat.assert_not_called()
        primary, secondary = probe('a',StatusError(401)), probe('b')
        self.assertEqual(fallback(primary, secondary).chat([])[0], 'ok')
        primary.chat.assert_called_once()
        primary, secondary = probe('a',StatusError(401)), probe('b',StatusError(503))
        with patch('openkyrozen.providers.retry.ProviderRequest.wait'), self.assertRaises(ProviderError):
            fallback(primary, secondary).chat([])
        self.assertEqual(primary.chat.call_count, 1)
        self.assertEqual(secondary.chat.call_count, 3)

    def test_contract_failure_does_not_retry_or_fallback(self):
        from openkyrozen.providers.models import ProviderContractError
        primary, secondary = probe('a', ProviderContractError('bad response')), probe('b')
        with self.assertRaises(ProviderContractError):
            fallback(primary, secondary).chat([])
        primary.chat.assert_called_once()
        secondary.chat.assert_not_called()

    def test_retry_after_is_bounded_and_bad_values_are_ignored(self):
        from openkyrozen.providers.errors import normalize_provider_error
        for value, expected in [('12',12), ('9000',600), ('-1',0), ('nan',None), ('bad',None)]:
            with self.subTest(value=value):
                self.assertEqual(normalize_provider_error(StatusError(429,headers={'Retry-After':value})).retry_after,expected)

    def test_stream_start_failures_share_budget_and_candidate_order(self):
        calls = []
        def make(name):
            provider = probe(name)
            def stream(*args):
                calls.append(name)
                raise StatusError(503)
                yield
            provider.chat_stream = stream
            return provider
        with patch('openkyrozen.providers.retry.ProviderRequest.wait'), self.assertRaises(ProviderError):
            list(fallback(make('a'),make('b')).chat_stream([]))
        self.assertEqual(calls,['a','b','a','b'])


class ReviewRegressions(unittest.TestCase):
    def test_httpx_timeout_and_transport_subclasses_remain_retryable(self):
        import httpx
        from openkyrozen.providers.errors import normalize_provider_error, ProviderErrorKind as K
        class CustomTimeout(httpx.ReadTimeout):
            pass
        for cls, kind in [(CustomTimeout,K.TIMEOUT),(httpx.WriteTimeout,K.TIMEOUT),(httpx.PoolTimeout,K.TIMEOUT),
                          (httpx.ReadError,K.TRANSPORT),(httpx.WriteError,K.TRANSPORT)]:
            with self.subTest(cls=cls):
                self.assertEqual(normalize_provider_error(cls('private')).kind,kind)
                call = Mock(side_effect=[cls('private'),'ok'])
                with patch('openkyrozen.providers.retry.ProviderRequest.wait'):
                    self.assertEqual(_retry_with_backoff(call),'ok')
                self.assertEqual(call.call_count,2)

    def test_received_stream_failing_before_text_never_retries_or_falls_back(self):
        import httpx
        from openkyrozen.providers.openai import OpenAICompatProvider
        from openkyrozen.providers.errors import ProviderError
        def acquired():
            yield NS(choices=[],usage=None)  # Successful stream, metadata only.
            raise httpx.ReadTimeout('private')
        primary = OpenAICompatProvider.__new__(OpenAICompatProvider)
        primary.config = ProviderConfig(provider='custom',model_simple='fixture')
        send = Mock(side_effect=lambda **kw: acquired())
        primary._client = NS(chat=NS(completions=NS(create=send)))
        secondary = probe('secondary')
        secondary.chat_stream = Mock(return_value=iter(['replayed']))
        with patch('openkyrozen.providers.retry.ProviderRequest.wait'), self.assertRaises(ProviderError):
            list(fallback(primary,secondary).chat_stream([]))
        send.assert_called_once()
        secondary.chat_stream.assert_not_called()

    def test_malformed_sdk_error_metadata_does_not_mask_original_failure(self):
        from openkyrozen.providers.errors import normalize_provider_error, ProviderErrorKind as K
        error = StatusError(503)
        for metadata in (None, ['bad'], 'bad'):
            error.response = {'ResponseMetadata':metadata,'Error':['bad']}
            normalized = normalize_provider_error(error)
            self.assertEqual(normalized.kind,K.SERVER)
            self.assertIs(normalized.__cause__,error)

    def test_runtime_entry_and_nested_fallback_share_transport_attempt_budget(self):
        from openkyrozen.providers.base import get_model_response
        from openkyrozen.providers.retry import provider_request_scope
        from openkyrozen.providers.errors import ProviderError
        for nested in (False,True):
            calls = []
            def make(name):
                p = probe(name)
                def fail(*args):
                    calls.append(name)
                    raise StatusError(503)
                p.chat = fail
                return p
            a,b=make('a'),make('b')
            wrapper=fallback(a,b)
            if nested:
                wrapper=fallback(wrapper)
            with self.subTest(nested=nested), provider_request_scope(timeout=10), \
                 patch('openkyrozen.providers.retry.ProviderRequest.wait'), self.assertRaises(ProviderError):
                get_model_response(wrapper,[])
            self.assertEqual(calls,['a','b','a','b'])

    def test_untrusted_provider_codes_are_not_displayed(self):
        from openkyrozen.providers.errors import normalize_provider_error
        for code in ('AIzaSyntheticCredentialValueForReview','sk-secret','private-request-body'):
            error = normalize_provider_error(StatusError(401,code))
            self.assertNotIn(code,str(error))

    def test_local_request_errors_do_not_retry_as_transport_failures(self):
        import httpx
        import requests
        try:
            from google.auth.exceptions import TransportError
        except ImportError:
            TransportError = None
        from openkyrozen.providers.errors import normalize_provider_error, ProviderErrorKind as K
        cases = [(httpx.LocalProtocolError('private'),K.INVALID_REQUEST),
                 (httpx.UnsupportedProtocol('private'),K.INVALID_REQUEST),
                 (requests.exceptions.InvalidURL('private'),K.INVALID_REQUEST)]
        if TransportError is not None:
            cases.append((TransportError('private'),K.TRANSPORT))
        for source, kind in cases:
            with self.subTest(source=type(source)):
                self.assertEqual(normalize_provider_error(source).kind,kind)

    def test_dynamic_callable_attributes_do_not_claim_retry_ownership(self):
        from openkyrozen.providers.base import get_model_response
        from openkyrozen.providers.models import ModelResponse
        send = Mock(side_effect=[StatusError(503),ModelResponse(text='ok')])
        provider = NS(chat_response=send,name='foreign')
        with patch('openkyrozen.providers.retry.ProviderRequest.wait'):
            self.assertEqual(get_model_response(provider,[]).text,'ok')
        self.assertEqual(send.call_count,2)

    def test_fallback_honors_retry_after_from_every_transient_candidate(self):
        for streaming in (False,True):
            primary = probe('a',StatusError(429,headers={'Retry-After':'12'}))
            secondary = probe('b',StatusError(503))
            if streaming:
                def first(*args):
                    raise StatusError(429,headers={'Retry-After':'12'})
                    yield
                def second(*args):
                    raise StatusError(503)
                    yield
                primary.chat_stream,secondary.chat_stream=first,second
            with self.subTest(streaming=streaming), patch('openkyrozen.providers.retry.ProviderRequest.wait') as wait, self.assertRaises(ProviderError):
                if streaming:
                    list(fallback(primary,secondary).chat_stream([]))
                else:
                    fallback(primary,secondary).chat_response([])
            wait.assert_called_once()
            self.assertGreaterEqual(wait.call_args.args[0],12)
