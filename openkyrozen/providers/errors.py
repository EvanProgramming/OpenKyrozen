"""SDK-independent, safe provider transport diagnostics."""
from __future__ import annotations

from enum import StrEnum
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import math
import re

from openkyrozen.providers.models import ProviderContractError


class ProviderErrorKind(StrEnum):
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    CONTEXT_OVERFLOW = "context_overflow"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


_TRANSIENT = {ProviderErrorKind.TRANSPORT, ProviderErrorKind.TIMEOUT,
              ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.SERVER}
_CONTEXT_MARKERS = ("context length", "context window", "maximum context", "max context",
                    "prompt is too long", "input is too long", "too many tokens", "token limit",
                    "exceeds the context", "exceeded context", "context_limit",
                    "exceeds the maximum number of tokens")
_CONTEXT_CODES = {"context_length_exceeded", "context_window_exceeded", "context_limit",
                  "input_too_long", "prompt_too_long", "too_many_tokens"}


def _safe_code(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value) and not value.lower().startswith(("sk-", "pk-")):
        return value
    return None


class ProviderError(RuntimeError):
    def __init__(self, kind: ProviderErrorKind, *, provider: str | None = None,
                 model: str | None = None, status_code: int | None = None,
                 provider_code: str | None = None, retry_after: float | None = None,
                 attempts: int = 0, terminal: bool = False):
        self.kind = ProviderErrorKind(kind)
        self.provider, self.model = provider, model
        self.status_code, self.provider_code = status_code, _safe_code(provider_code)
        self.retry_after = retry_after
        self.attempts = attempts
        self.retryable = self.kind in _TRANSIENT and not terminal
        self.terminal = terminal
        label = "Provider timed out" if self.kind == ProviderErrorKind.TIMEOUT else f"Provider {self.kind.value} failure"
        details = [f"HTTP {status_code}" if status_code else None]
        super().__init__(label + (" (" + ", ".join(x for x in details if x) + ")" if any(details) else ""))


def legacy_context_overflow(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return exc.kind == ProviderErrorKind.CONTEXT_OVERFLOW
    return any(marker in str(exc).lower() for marker in _CONTEXT_MARKERS)


def _retry_after(headers) -> float | None:
    raw = (headers.get("retry-after") or headers.get("Retry-After")) if hasattr(headers, "get") else None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(600.0, max(0.0, seconds)) if math.isfinite(seconds) else None


def normalize_provider_error(exc: Exception, provider=None, model=None) -> ProviderError:
    if isinstance(exc, ProviderContractError):
        raise exc
    if isinstance(exc, ProviderError):
        if exc.provider is None:
            exc.provider = provider
        if exc.model is None:
            exc.model = model
        return exc
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    if isinstance(response, dict):  # botocore ClientError
        status = metadata.get("HTTPStatusCode", status)
        body = response.get("Error", {})
    elif response is not None:
        status = getattr(response, "status_code", status)
        if body is None and callable(getattr(response, "json", None)):
            try:
                body = response.json()
            except Exception:
                pass
    if body is None:
        body = getattr(exc, "details", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    code = None
    if isinstance(body, dict):
        code = body.get("code") if isinstance(body.get("code"), str) else None
        code = code or body.get("Code") or body.get("status") or body.get("type")
    sdk_modules = {cls.__module__.split(".")[0] for cls in type(exc).__mro__}
    if "google" in sdk_modules and status is None:
        status = getattr(exc, "code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    code_key = str(code or "").lower()
    names = {cls.__name__ for cls in type(exc).__mro__}
    known_sdk = bool(sdk_modules & {"openai", "anthropic", "perplexity", "google", "botocore", "requests", "httpx", "azure"})
    message = str(exc)
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("Message") or body.get("error") or message)
    if code_key in _CONTEXT_CODES or ((status in {400, 413, 422} or (status is None and not known_sdk)) and legacy_context_overflow(RuntimeError(message))):
        kind = ProviderErrorKind.CONTEXT_OVERFLOW
    elif status == 499 or code_key in {"cancelled", "canceled"}:
        kind = ProviderErrorKind.CANCELLED
    elif (known_sdk and names & {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "RefreshError", "DefaultCredentialsError", "ClientAuthenticationError", "CredentialUnavailableError"}) or status in {401, 403} or code_key in {"unauthenticated", "permission_denied", "unrecognizedclientexception", "accessdeniedexception", "invalid_api_key"}:
        kind = ProviderErrorKind.AUTHENTICATION
    elif status == 429 or code_key in {"resource_exhausted", "throttlingexception", "toomanyrequestsexception"}:
        kind = ProviderErrorKind.RATE_LIMIT
    elif known_sdk and names & {"LocalProtocolError", "UnsupportedProtocol", "InvalidURL", "InvalidSchema", "MissingSchema", "InvalidHeader"}:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif status in {408, 504} or isinstance(exc, TimeoutError) or (known_sdk and names & {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "ReadTimeoutError", "ConnectTimeoutError", "Timeout", "TimeoutException"}):
        kind = ProviderErrorKind.TIMEOUT
    elif status is not None and 500 <= status <= 599:
        kind = ProviderErrorKind.SERVER
    elif (status is not None and 400 <= status <= 499) or code_key in {"invalid_argument", "validationexception"}:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif isinstance(exc, ConnectionError) or (known_sdk and names & {"APIConnectionError", "ConnectionError", "ConnectError", "NetworkError", "EndpointConnectionError", "ConnectionClosedError", "RemoteProtocolError", "TransportError", "ChunkedEncodingError"}):
        kind = ProviderErrorKind.TRANSPORT
    else:
        kind = ProviderErrorKind.UNKNOWN
    headers = metadata.get("HTTPHeaders", {}) if isinstance(response, dict) else getattr(response, "headers", {})
    error = ProviderError(kind, provider=provider, model=model, status_code=status,
                          provider_code=code, retry_after=_retry_after(headers))
    error.__cause__ = exc
    return error
