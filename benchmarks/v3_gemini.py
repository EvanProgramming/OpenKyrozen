"""Native Google benchmark transport and immutable account-aware money policy.

This module is benchmark-only. Product providers retain their own selection and
wire messages; only the SDK endpoint is replaced inside isolated workers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import copy
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler
import requests
from benchmarks.v3_responses_relay import ResponsesRelay, RelayConfig, MAX_REQUEST_BYTES

from benchmarks.v3_matched import Pricing

OFFICIAL_ENDPOINT = "https://generativelanguage.googleapis.com"
INPUT_BOUND = 1_048_576  # Financial reservation covers each pinned backend's full input maximum.
OPERATIONAL_INPUT_BOUND = 1_000_000
OUTPUT_BOUND = 4096
CONSERVATIVE_CNY_USD = Decimal("8")
REFERENCE_CNY_USD = Decimal("6.7188")
PRICING_SOURCE_SHA256 = "ad2140dd935ccca0a66bb70f7c1b59b5e4c74d6180b599a8c32e0dcf4a422a2e"
MODEL_METADATA_SHA256 = "5a727200c71927a21e7702332c1715e60f9e6d82a28fa51633e5e6797e24b92e"
MODEL_VERSIONS = {
    "gemini-2.5-flash": "001",  # Generic metadata tag; not a synthesized served-model alias.
    "gemini-3.5-flash-lite": "3.5-flash-lite-07-2026",
    "gemini-3.1-pro-preview": "3.1-pro-preview-01-2026",
    "gemini-3.8-flash": "3.0",  # Generic metadata version is NOT a served-model alias.
}


@dataclass(frozen=True)
class GeminiPrice(Pricing):
    cache_available: bool = True

    def cost(self, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
        if cache_write_tokens or (cache_read_tokens and not self.cache_available):
            raise ValueError("Google cache creation/storage or unavailable cache pricing is forbidden")
        return super().cost(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)


@dataclass(frozen=True)
class GeminiPricing:
    """Model/date prices, with a fixed conservative CNY upper rather than invoice FX."""
    billing_tier: str = "paid"
    max_limit_cny: Decimal | None = Decimal("15")

    def __post_init__(self):
        if self.billing_tier not in {"free", "paid"} or self.max_limit_cny not in (None, Decimal("15")):
            raise ValueError("unknown Google billing tier or changed cumulative cap")

    def as_dict(self):
        return {"currency": "CNY", "provider": "Google native", "source_currency": "USD",
                "billing_tier": self.billing_tier,
                "max_limit_cny": str(self.max_limit_cny) if self.max_limit_cny is not None else None,
                "billing_tier_semantics": "standard paid rate upper, or verified account-free rates",
                'financial_input_reservation':INPUT_BOUND,'operational_input_token_limit':OPERATIONAL_INPUT_BOUND,
                "cost_basis": "conservative CNY upper; not invoice conversion",
                "conservative_cny_per_usd": "8", "reference_cny_per_usd": "6.7188",
                "pricing_source_sha256": PRICING_SOURCE_SHA256,
                "model_metadata_sha256": MODEL_METADATA_SHA256,
                "models": {"gemini-2.5-flash": {"paid": ["0.30", "2.50", "0.03"],
                            "free": ["0", "0", None], "automatic_reroute_cutoff": "2026-12-01"},
                           "gemini-3.5-flash-lite": {"paid": ["0.30", "2.50", "0.03"],
                            "free": ["0", "0", None]},
                           "gemini-3.1-pro-preview": {"paid_le_200000": ["2", "12", "0.20"],
                            "paid_gt_200000": ["4", "18", "0.40"], "free": None},
                           "gemini-3.8-flash": {"paid_through_2026": ["0.75", "3.75", "0.075"],
                            "paid_from_2027": ["1.50", "7.50", "0.15"], "free": ["0", "0", "0"]}}}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def for_metadata(self, metadata, *, input_tokens):
        if not isinstance(metadata, dict) or type(input_tokens) is not int or input_tokens < 0:
            raise ValueError("Google price requires trusted model/tier/date metadata")
        model, tier = metadata.get("model"), metadata.get("billing_tier")
        if model not in MODEL_VERSIONS or tier != self.billing_tier:
            raise ValueError("unknown Google model or billing tier")
        try:
            day = date.fromisoformat(metadata["price_date"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Google price date is invalid") from None
        if day < date(2026, 11, 9):
            raise ValueError("Google catalog does not establish earlier prices")
        if model == 'gemini-2.5-flash' and day >= date(2026,12,1):
            raise ValueError('Gemini 2.5 Flash automatic reroute cutoff would degenerate the role policy')
        row = self.as_dict()["models"][model]
        if tier == "free":
            rates = row["free"]
        elif model == "gemini-3.1-pro-preview":
            rates = row["paid_gt_200000" if input_tokens > 200_000 else "paid_le_200000"]
        elif model == "gemini-3.8-flash":
            rates = row["paid_through_2026" if day.year == 2026 else "paid_from_2027"]
        else:
            rates = row["paid"]
        if rates is None:
            raise ValueError("Google model is unavailable under this billing tier")
        input_rate, output_rate, cache_rate = [Decimal(value or "0") * CONSERVATIVE_CNY_USD for value in rates]
        details = {"currency": "CNY", "input_per_million": str(input_rate),
                   "output_per_million": str(output_rate), "cache_read_per_million": str(cache_rate),
                   "cache_write_per_million": str(input_rate), "model": model, "billing_tier": tier,
                   "price_date": day.isoformat(), "catalog_sha256": self.sha256}
        return GeminiPrice(input_rate, output_rate, cache_rate, input_rate, self.max_limit_cny,
                           json.dumps(details, sort_keys=True), rates[2] is not None)

    def cost(self, *_args):
        raise ValueError("Google automatic selection has no uniform model price")

    def reference_cost(self, metadata, input_tokens, output_tokens):
        return (self.for_metadata(metadata, input_tokens=input_tokens).cost(input_tokens, output_tokens)
                / CONSERVATIVE_CNY_USD * REFERENCE_CNY_USD)

    def row_cost(self, row):
        calls = row.get("cost_calls")
        if not isinstance(calls, list) or not calls:
            raise ValueError("Google row lacks its per-physical-call cost journal")
        total = Decimal(0)
        for call in calls:
            if call.get("status") != "settled" or not isinstance(call.get("usage"), dict):
                raise ValueError("Google row has unresolved physical calls")
            usage = call["usage"]
            raw = native_usage({'usageMetadata': usage.get('raw_usage')})
            if (raw is None or any(usage.get(key) != value for key, value in raw.items())
                    or not served_identity(call['model'], {'modelVersion': usage.get('served_model')})):
                raise ValueError('Google usage lacks its complete native counters or served identity')
            cost = self.for_metadata(call, input_tokens=usage["input_tokens"]).cost(
                *(usage[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")))
            if Decimal(call["actual_rmb"]) != cost:
                raise ValueError("Google physical cost does not match immutable pricing")
            total += cost
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            if sum(call["usage"][key] for call in calls) != row[key]:
                raise ValueError("Google row aggregate does not match physical usage")
        return total


class GeminiPolicy:
    protocol = "google"
    model = "auto"
    smoke_calls = 4

    def __init__(self, *, config=None, pricing=None, price_date=None):
        from openkyrozen.providers.config import ProviderConfig
        self.config = config or ProviderConfig(provider="google", model_main="auto")
        self.pricing = pricing or GeminiPricing()
        self.price_date = price_date or datetime.now(timezone.utc).date().isoformat()
        self.role_models = {"simple": self.config.model_simple, "complex": self.config.model_complex}
        if (self.config.provider != "google" or self.config.model_main != "auto"
                or len(set(self.role_models.values())) != 2
                or any(model not in MODEL_VERSIONS for model in self.role_models.values())):
            raise ValueError("Google automatic policy requires two distinct native role models and model_main=auto")
        self.billing_record = None
        self.billing_proof_sha256 = None
        if 'gemini-2.5-flash' in self.role_models.values():
            self.pricing.for_metadata(self.metadata('gemini-2.5-flash'),input_tokens=INPUT_BOUND)

    def metadata(self, model):
        if model not in self.role_models.values():
            raise ValueError("model is outside the immutable Google role policy")
        value = {"model": model, "billing_tier": self.pricing.billing_tier, "price_date": self.price_date,
                 "pricing_sha256": self.pricing.sha256}
        if self.billing_proof_sha256 is not None:
            value['billing_proof_sha256'] = self.billing_proof_sha256
            value['account_billing_status'] = self.billing_record['account_billing_status']
            value['rate_policy'] = self.billing_record['rate_policy']
        return value

    def identity(self):
        return {"provider": "google", "protocol": "google-native", "model_main": "auto",
                "role_models": dict(self.role_models), "price_date": self.price_date,
                "pricing_sha256": self.pricing.sha256, "model_metadata_sha256": MODEL_METADATA_SHA256,
                "model_versions": {model: MODEL_VERSIONS[model] for model in self.role_models.values()},
                "reservation_input_tokens": INPUT_BOUND,'operational_input_token_limit':OPERATIONAL_INPUT_BOUND,
                "max_output_tokens_including_thoughts": OUTPUT_BOUND}

    def accepts_call(self, call):
        try:
            expected = self.metadata(call.get("model"))
            if not all(call.get(key) == value for key,value in expected.items()) or call.get('max_output_tokens')!=OUTPUT_BOUND:
                return False
            usage=call.get('usage')
            if usage is None:
                return call.get('status') not in {'settled','overrun'}
            return (isinstance(usage,dict) and type(usage.get('input_tokens')) is int
                and 0<=usage['input_tokens']<=OPERATIONAL_INPUT_BOUND
                and type(usage.get('output_tokens')) is int and 0<=usage['output_tokens']<=OUTPUT_BOUND)
        except (ValueError, AttributeError):
            return False

    def credential(self):
        # Read only after runner OS, source, budget and signed-account admission checks.
        key = os.environ.get("KYROZEN_BENCHMARK_API_KEY", "")
        self.check_credential(key)
        return OFFICIAL_ENDPOINT, key

    def check_budget(self, budget):
        for model in self.role_models.values():
            quote = self.pricing.for_metadata(self.metadata(model), input_tokens=INPUT_BOUND).cost(INPUT_BOUND, OUTPUT_BOUND)
            if not budget.budget_ok or (budget.remaining_rmb is not None and quote > budget.remaining_rmb):
                raise ValueError("Google both-slot reservation is unaffordable before credentials or inference")

    def verify_billing(self, proof, candidate_sha, *, verify_signature=None, now=None):
        from benchmarks.v3_release_gates import _read_signed_pair, _gpg_verify
        self.billing_record = None
        self.billing_proof_sha256 = None
        if proof is None:
            raise ValueError("Google billing proof is required before credentials or inference")
        record_path = Path(proof)
        raw, signature, verified = _read_signed_pair(record_path, Path(str(record_path) + ".asc"),
                                                    verify_signature or _gpg_verify)
        record = json.loads(raw)
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        now = now or datetime.now(timezone.utc)
        try:
            issued = datetime.fromisoformat(record["issued_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Google billing proof has invalid validity dates") from None
        if (not verified or raw != canonical or not signature or issued.tzinfo is None or expires.tzinfo is None
                or not issued <= now < expires or now.date().isoformat()!=self.price_date or record.get("schema_version") != 1
                or record.get("provider") != "google" or record.get("official_endpoint") != OFFICIAL_ENDPOINT
                or record.get("candidate_sha") != candidate_sha or record.get("role_models") != self.role_models
                or record.get("model_main") != "auto" or record.get("billing_tier") != self.pricing.billing_tier
                or not re.fullmatch(r"[0-9a-f]{64}", record.get("credential_sha256", ""))
                or (self.pricing.billing_tier == "free" and record.get("account_unbilled_models") != sorted(self.role_models.values()))
                or (self.pricing.billing_tier == 'free' and (record.get('account_billing_status')!='free_verified'
                    or record.get('rate_policy')!='account_free'))
                or (self.pricing.billing_tier == "paid" and (record.get("conservative_cny_per_usd") != "8"
                    or record.get('account_billing_status') not in {'unverified','paid_verified'}
                    or record.get('rate_policy')!='standard_paid_rate_upper'))):
            raise ValueError("Google signed billing proof does not bind this candidate/account/policy")
        supporting = record.get("supporting_evidence")
        if not isinstance(supporting, list) or not supporting:
            raise ValueError("Google billing proof lacks nonsecret supporting evidence")
        for row in supporting:
            path = Path(row["path"])
            if path.is_symlink() or not path.is_file():
                raise ValueError("Google billing evidence is not a regular file")
            data = path.read_bytes()
            if len(data) != row.get("bytes") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
                raise ValueError("Google billing supporting evidence changed")
        self.billing_record = record
        self.billing_proof_sha256 = hashlib.sha256(raw).hexdigest()
        return {"proof_sha256": hashlib.sha256(raw).hexdigest(), "signature_sha256": hashlib.sha256(signature).hexdigest(),
                "issued_at": record["issued_at"], "expires_at": record["expires_at"],
                "billing_tier": self.pricing.billing_tier,'account_billing_status':record['account_billing_status'],
                'rate_policy':record['rate_policy']}

    def check_credential(self, api_key):
        if (self.billing_record is None or not isinstance(api_key, str) or not api_key
                or hashlib.sha256(api_key.encode()).hexdigest() != self.billing_record["credential_sha256"]):
            raise ValueError("Google credential does not match the verified billing proof")

    def check_transport(self):
        now=datetime.now(timezone.utc)
        if (self.billing_record is None or self.billing_proof_sha256 is None
                or now.date().isoformat()!=self.price_date
                or now>=datetime.fromisoformat(self.billing_record['expires_at'].replace('Z','+00:00'))):
            raise ValueError('native_billing_proof_expired_or_absent')


def native_usage(result):
    usage = result.get("usageMetadata") if isinstance(result, dict) else None
    fields = {"promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "cachedContentTokenCount",
              "totalTokenCount", "toolUsePromptTokenCount", "promptTokensDetails", "cacheTokensDetails",
              "candidatesTokensDetails", "toolUsePromptTokensDetails", "trafficType", "serviceTier"}
    if not isinstance(usage, dict) or set(usage) - fields:
        return None
    service_tier=usage.get('serviceTier','unspecified')
    if not isinstance(service_tier,str) or service_tier not in {'standard','unspecified'}:
        return None
    prompt, candidates = usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    thoughts, cached = usage.get("thoughtsTokenCount", 0), usage.get("cachedContentTokenCount", 0)
    total, tool_prompt = usage.get("totalTokenCount"), usage.get("toolUsePromptTokenCount", 0)
    if (any(type(count) is not int or count < 0 for count in (prompt, candidates, thoughts, cached, total, tool_prompt))
            or cached > prompt or tool_prompt or total != prompt + candidates + thoughts
            or usage.get("trafficType", "ON_DEMAND") != "ON_DEMAND"):
        return None
    for field, expected in (('promptTokensDetails',prompt),('cacheTokensDetails',cached),
                            ('candidatesTokensDetails',candidates),('toolUsePromptTokensDetails',tool_prompt)):
        if field in usage:
            details=usage[field]
            if (not isinstance(details,list) or any(not isinstance(item,dict)
                    or set(item)!={'modality','tokenCount'} or item['modality']!='TEXT'
                    or type(item['tokenCount']) is not int or item['tokenCount']<0 for item in details)
                    or sum(item['tokenCount'] for item in details)!=expected):
                return None
    return {"input_tokens": prompt, "output_tokens": candidates + thoughts,
            "cache_read_tokens": cached, "cache_write_tokens": 0, "reasoning_tokens": thoughts}


def validate_native_body(body):
    """Admit native text/function messages, without translating signatures or tools."""
    if not isinstance(body, dict) or set(body) - {"contents", "systemInstruction", "tools", "toolConfig", "generationConfig"}:
        raise ValueError("unpriced Google request feature")
    value = copy.deepcopy(body)
    contents = value.get("contents")
    if not isinstance(contents, list) or not contents:
        raise ValueError("native contents must be nonempty")
    instructions = [value["systemInstruction"]] if "systemInstruction" in value else []
    for content in contents + instructions:
        if (not isinstance(content, dict) or set(content) - {"role", "parts"}
                or content.get("role", "user") not in {"user", "model", "system"}
                or not isinstance(content.get("parts"), list) or not content["parts"]):
            raise ValueError("invalid native text/function content")
        for part in content["parts"]:
            signature_only = (isinstance(part,dict) and set(part)<={'thoughtSignature','thought'}
                              and isinstance(part.get('thoughtSignature'),str) and bool(part['thoughtSignature']))
            if (not isinstance(part, dict) or set(part) - {"text", "functionCall", "functionResponse", "thoughtSignature", "thought"}
                    or (sum(key in part for key in ("text", "functionCall", "functionResponse")) != 1 and not signature_only)
                    or ("text" in part and not isinstance(part["text"], str))
                    or ('thoughtSignature' in part and not isinstance(part['thoughtSignature'],str))
                    or ('thought' in part and type(part['thought']) is not bool)):
                raise ValueError("only native text and function parts are priced")
            for name in ("functionCall", "functionResponse"):
                if name in part and (not isinstance(part[name], dict) or not isinstance(part[name].get("name"), str)):
                    raise ValueError("invalid native function part")
                if name in part and set(part[name])-({'id','name','args'} if name=='functionCall' else {'id','name','response'}):
                    raise ValueError('unpriced native function part')
    for tool in value.get("tools", []):
        if (not isinstance(tool, dict) or set(tool) != {"functionDeclarations"}
                or not isinstance(tool["functionDeclarations"], list)
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                       for item in tool["functionDeclarations"])):
            raise ValueError("unpriced Google builtin tool")
    tool_config = value.get("toolConfig", {})
    if (not isinstance(tool_config, dict) or set(tool_config) - {"functionCallingConfig"}
            or ("functionCallingConfig" in tool_config and (
                not isinstance(tool_config["functionCallingConfig"], dict)
                or set(tool_config["functionCallingConfig"]) - {"mode", "allowedFunctionNames"}))):
        raise ValueError("unpriced Google tool configuration")
    generation = value.setdefault("generationConfig", {})
    if (not isinstance(generation, dict) or set(generation) - {"temperature", "topP", "topK", "maxOutputTokens", "thinkingConfig"}
            or ("thinkingConfig" in generation and (not isinstance(generation["thinkingConfig"], dict)
                or set(generation["thinkingConfig"]) - {"includeThoughts", "thinkingBudget", "thinkingLevel"}))):
        raise ValueError("unpriced Google generation configuration or candidate fanout")
    generation["maxOutputTokens"] = OUTPUT_BOUND
    return value


def served_identity(model, response):
    version = MODEL_VERSIONS[model]
    allowed = {model}
    if version.startswith(model.removeprefix("gemini-") + "-"):
        allowed.add("gemini-" + version)
    return isinstance(response, dict) and response.get("modelVersion") in allowed


def make_google_provider(config):
    """Use each checked-out product provider unchanged, with SDK transport local only."""
    from openkyrozen.providers import get_provider
    from benchmarks.v3_actor_worker import benchmark_google_provider
    return benchmark_google_provider(config, get_provider)


class GeminiRelay(ResponsesRelay):
    """Native generateContent gateway sharing the existing trusted actor authority."""
    protocol = "google"

    def __init__(self, *, endpoint, api_key, budget, policy):
        from urllib.parse import urlsplit
        parsed=urlsplit(endpoint)
        self.official=endpoint==OFFICIAL_ENDPOINT
        if not self.official and (parsed.scheme!='http' or parsed.hostname!='127.0.0.1'
                                  or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise ValueError('native upstream must be official Google or a synthetic loopback fixture')
        if self.official:
            policy.check_transport()
            policy.check_credential(api_key)
        if budget.pricing.sha256 != policy.pricing.sha256:
            raise ValueError("Google gateway and ledger catalogs differ")
        self.policy = policy
        self._selected_models = {}
        self.root_actor_id_by_label = {}
        super().__init__(endpoint=endpoint, api_key=api_key, budget=budget,
                         config=RelayConfig(model="auto", max_output_tokens=OUTPUT_BOUND,
                                            fixed_input_token_bound=INPUT_BOUND, serial_requests=True))

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_port}"

    def issue_actor_config(self, actor_id):
        config = super().issue_actor_config(actor_id)
        with self._lock:
            self._selected_models={key:value for key,value in self._selected_models.items() if key[0]!=actor_id}
        config.update(provider="google", model_main="auto", model_simple=self.policy.role_models["simple"],
                      model_complex=self.policy.role_models["complex"],context_window_tokens=OPERATIONAL_INPUT_BOUND)
        return config

    def revoke_actor(self, actor_id):
        super().revoke_actor(actor_id)
        with self._lock:
            self._selected_models={key:value for key,value in self._selected_models.items() if key[0]!=actor_id}

    def selected_actor_model(self, actor_id):
        with self._lock:
            active = any(row["actor_id"] == actor_id and row["label"] == tuple(self.label)
                         for row in self._actor_credentials.values())
            model = self._selected_models.get((actor_id, tuple(self.label)))
            if not active or self.provider_error or model not in self.policy.role_models.values():
                raise ValueError("parent actor has no admitted selected model")
            return model

    def _handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, code, body):
                payload = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                with relay._request_lock, relay._upstream_lock:
                    self._forward()

            def _forward(self):
                route = re.fullmatch(r"/v1beta/models/(gemini-[a-zA-Z0-9.-]+):(generateContent|streamGenerateContent)(\?alt=sse)?", self.path)
                if not route or (route[2] == "generateContent" and route[3]):
                    self._json(404, {"error": {"message": "unsupported native benchmark route"}})
                    return
                auth = "Bearer " + (self.headers.get("x-goog-api-key") or "")
                authorized, actor_id, label = relay._authorize_client(auth)
                if not authorized:
                    self._json(401, {"error": {"message": "invalid benchmark actor credential"}})
                    return
                model, streaming = route[1], route[2] == "streamGenerateContent"
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BYTES:
                        raise ValueError("request bound")
                    body = validate_native_body(json.loads(self.rfile.read(length)))
                    metadata = relay.policy.metadata(model)
                    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                    if len(encoded)+512>OPERATIONAL_INPUT_BOUND:
                        raise ValueError('native text bytes exceed conservative fixed input reservation')
                except (ValueError, TypeError):
                    self._json(400, {"error": {"message": "native request is outside the pinned text/function policy"}})
                    return
                arm, fixture_id, repeat = label
                quote = relay.policy.pricing.for_metadata(metadata, input_tokens=INPUT_BOUND).cost(INPUT_BOUND, OUTPUT_BOUND)
                row = dict(metadata, attempt_id=uuid.uuid4().hex, arm=arm, fixture_id=fixture_id, repeat=repeat,
                           request_sha256=hashlib.sha256(encoded).hexdigest(), max_output_tokens=OUTPUT_BOUND,
                           reserved_rmb=str(quote), status="unknown", usage=None, protocol="google-native",
                           admission_sequence=len(relay.rows))
                if actor_id is not None:
                    row["actor_id"] = actor_id
                    root=relay.root_actor_id_by_label.get(label)
                    if root is not None:
                        row.update(root_actor_id=root,actor_role='root' if actor_id==root else 'descendant')
                upstream, reserved, sent = None, False, False
                started = time.monotonic()
                try:
                    if relay.official:
                        relay.policy.check_transport()
                    if relay._authorize_client(auth) != (True, actor_id, label):
                        self._json(401, {"error": {"message": "actor authority changed before admission"}})
                        return
                    if relay.provider_error or (label == relay.planning_preflight_label and relay.planning_preflight_closed):
                        self._json(503, {"error": {"message": "benchmark admission is closed"}})
                        return
                    if label == relay.planning_preflight_label and sum(
                            (item.get("arm"), item.get("fixture_id"), item.get("repeat")) == label for item in relay.rows) >= 6:
                        relay.provider_error = "planning_preflight_request_limit"
                        self._json(429, {"error": {"message": relay.provider_error}})
                        return
                    if not relay.budget.reserve(row["attempt_id"], input_token_bound=INPUT_BOUND,
                                                output_token_bound=OUTPUT_BOUND, metadata=row):
                        relay.budget_blocked = True
                        self._json(402, {"error": {"message": "benchmark spend cap reached"}})
                        return
                    reserved = True
                    with relay._lock:
                        relay.rows.append(row)
                        if actor_id is not None:
                            relay._selected_models[(actor_id, label)] = model
                    upstream = requests.post(relay.endpoint + self.path, data=encoded,
                        headers={"x-goog-api-key": relay.api_key, "Content-Type": "application/json"},
                        timeout=180, allow_redirects=False, stream=True)
                    row["http_status"] = upstream.status_code
                    if upstream.status_code != 200:
                        raise ValueError(f"upstream_http_{upstream.status_code}")
                    usage, terminal, received = None, False, 0
                    if not streaming:
                        chunks = []
                        for chunk in upstream.iter_content(chunk_size=4096):
                            received += len(chunk)
                            if received > MAX_REQUEST_BYTES:
                                raise ValueError("native_response_bound")
                            chunks.append(chunk)
                        result = json.loads(b"".join(chunks))
                        if not served_identity(model, result) or len(result.get("candidates", [])) != 1:
                            raise ValueError("unknown_served_model_or_candidate_count")
                        usage = native_usage(result)
                        usage_result = result
                        terminal = bool(result["candidates"][0].get("finishReason"))
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        sent = True
                        frame = bytearray()
                        for chunk in upstream.iter_content(chunk_size=4096):
                            received += len(chunk)
                            if received > MAX_REQUEST_BYTES:
                                raise ValueError("native_response_bound")
                            frame.extend(chunk)
                            while b"\n\n" in bytes(frame).replace(b"\r\n", b"\n"):
                                event, _, rest = bytes(frame).replace(b"\r\n", b"\n").partition(b"\n\n")
                                frame = bytearray(rest)
                                data = b"\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
                                if not data:
                                    continue
                                result = json.loads(data)
                                if not served_identity(model, result) or len(result.get("candidates", [])) > 1:
                                    raise ValueError("unknown_served_model_or_candidate_count")
                                if "usageMetadata" in result:
                                    usage = native_usage(result)
                                    usage_result = result
                                    if usage is None:
                                        raise ValueError("unknown_native_usage")
                                terminal = terminal or any(candidate.get("finishReason") for candidate in result.get("candidates", []))
                                self.wfile.write(event + b"\n\n")
                                self.wfile.flush()
                        if frame.strip():
                            raise ValueError("incomplete_native_stream")
                    if usage is None or not terminal:
                        raise ValueError("missing_native_usage_or_terminal")
                    usage["served_model"] = usage_result["modelVersion"]
                    usage['raw_usage'] = copy.deepcopy(usage_result['usageMetadata'])
                    relay.budget.settle(row["attempt_id"], usage)
                    row.update(usage=usage, status="settled" if relay.budget.budget_ok else "overrun", served_model=result["modelVersion"],
                               actual_rmb=str(relay.policy.pricing.for_metadata(metadata, input_tokens=usage["input_tokens"]).cost(
                                   usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"])))
                    if not relay.budget.budget_ok or usage["input_tokens"] > OPERATIONAL_INPUT_BOUND or usage["output_tokens"] > OUTPUT_BOUND:
                        relay.provider_error = "native_usage_overrun"
                    if not streaming:
                        self._json(200, result)
                except (ValueError, TypeError, KeyError, requests.RequestException, OSError) as exc:
                    # Never propagate upstream text, auth or request bodies into evidence/errors.
                    relay.provider_error = (str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_0-9]+", str(exc))
                                            else "native_" + type(exc).__name__)
                    row["provider_error"] = relay.provider_error
                    if not sent:
                        try:
                            self._json(502, {"error": {"message": "native benchmark upstream failed validation or transport"}})
                        except OSError:
                            pass
                finally:
                    if reserved and row["status"] == "unknown":
                        relay.budget.settle(row["attempt_id"], None)
                    if upstream is not None:
                        upstream.close()
                    row["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                    self.close_connection = True

        return Handler


def run_smoke(relay, candidate):
    """Four native physical calls: function then continuation for EACH immutable role."""
    from google import genai
    prior = getattr(relay, "smoke_verified_candidate", None)
    if prior == candidate:
        return True
    relay.label = ("smoke", candidate, -1)
    before = len(relay.rows)
    client = genai.Client(api_key=relay.client_key, http_options={"base_url": relay.url,
        "api_version": "v1beta", "retry_options": {"attempts": 1}, "timeout": 180_000})
    try:
        for model in relay.policy.role_models.values():
            tools=[{"function_declarations": [{"name": "benchmark_echo",
                "parameters": {"type": "OBJECT", "properties": {"value": {"type": "STRING"}}, "required": ["value"]}}]}]
            first = client.models.generate_content(model=model, contents="Call benchmark_echo with value ready.",
                config={"tools": tools,
                    "tool_config": {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["benchmark_echo"]}}})
            parts = first.candidates[0].content.parts
            calls = [part.function_call for part in parts if part.function_call is not None]
            if len(calls) != 1 or calls[0].name != "benchmark_echo" or calls[0].args != {"value": "ready"}:
                raise ValueError("native_smoke_function_contract")
            function_response={"name":"benchmark_echo","response":{"value":"ready"}}
            if calls[0].id is not None:
                function_response['id']=calls[0].id
            second = client.models.generate_content(model=model, contents=[
                {"role": "user", "parts": [{"text": "Call benchmark_echo with value ready."}]},
                first.candidates[0].content,
                {"role": "user", "parts": [{"function_response": function_response}]}],
                config={'tools':tools,'tool_config':{'function_calling_config':{'mode':'NONE'}}})
            if (not second.text or "ready" not in second.text.lower()
                    or any(part.function_call is not None for part in second.candidates[0].content.parts)):
                raise ValueError("native_smoke_continuation_contract")
        calls = relay.rows[before:]
        if len(calls) != 4 or any(call["status"] != "settled" for call in calls):
            raise ValueError("native_smoke_usage_contract")
        relay.smoke_verified_candidate = candidate
        relay.smoke_verified_attempt_ids = tuple(call["attempt_id"] for call in calls)
        return True
    except Exception as exc:
        relay.provider_error = relay.provider_error or "native_smoke_" + type(exc).__name__
        return False
    finally:
        client.close()


def main():
    import argparse
    from openkyrozen.providers.config import ProviderConfig
    from benchmarks.v3_deepseek import read_prior_exposure
    from benchmarks.v3_matched_live import run, _source_snapshot
    from scripts.publish_v3_preview import _verify_candidate_signature
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','repo','output','budget-db'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--candidate-sha',required=True)
    parser.add_argument('--prior-ledger',type=Path,action='append')
    parser.add_argument('--core-output',type=Path)
    parser.add_argument('--core-budget',type=Path)
    parser.add_argument('--model-routing',action='store_true',help='Run the separate36 contrast from completed original54 origin')
    parser.add_argument('--billing-proof',type=Path)
    parser.add_argument('--billing-tier',choices=('free','paid'),required=True)
    parser.add_argument('--model-simple')
    parser.add_argument('--model-complex')
    parser.add_argument('--limit-cny',default='15',help='Cumulative CNY cap (default15), or explicit unlimited accounting')
    parser.add_argument('--planning-preflight-only',action='store_true')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        parser.error('--execute is required; imports and offline tests never contact Google')
    candidate=_source_snapshot(args.source.resolve())
    if candidate!=args.candidate_sha:
        raise ValueError('source does not match frozen native Google candidate')
    _verify_candidate_signature(args.source.resolve(),candidate)
    config=ProviderConfig(provider='google',model_main='auto',**{
        key:value for key,value in {'model_simple':args.model_simple,'model_complex':args.model_complex}.items() if value is not None})
    policy=GeminiPolicy(config=config,pricing=GeminiPricing(billing_tier=args.billing_tier,
        max_limit_cny=None if args.limit_cny=='unlimited' else Decimal(15)))
    if args.core_output is not None or args.core_budget is not None:
        if args.core_output is None or args.core_budget is None or args.planning_preflight_only or args.prior_ledger:
            raise ValueError('routing supplement requires both original core paths without new prior admission')
        from benchmarks.v3_routing_supplement import run as routing_run
        report=routing_run(source=args.source,repo=args.repo,core_output=args.core_output,core_budget=args.core_budget,
            output=args.output,budget_path=args.budget_db,provider_policy=policy,billing_proof=args.billing_proof,
            model_routing=args.model_routing)
        print(json.dumps(report['summary'],sort_keys=True,indent=2))
        raise SystemExit(0 if report.get('status')=='complete' and report['summary'].get('benefit_passed') is True else 1)
    if args.model_routing:
        raise ValueError('model-routing requires its completed54 core-output/core-budget origin')
    if not args.prior_ledger:
        raise ValueError('original prior ledgers are required for the shared cumulative campaign')
    exposure=read_prior_exposure(args.prior_ledger)
    limit=None if args.limit_cny=='unlimited' else Decimal(args.limit_cny)
    if limit is not None and (not limit.is_finite() or not 0<limit<=15 or exposure.total_cny>limit):
        raise ValueError('native Google campaign requires a cap up to 15 CNY or explicit unlimited accounting')
    report=run(source=args.source,repo=args.repo,output=args.output,budget_path=args.budget_db,
        limit_cny='unlimited' if limit is None else str(limit),prior_hold_cny=str(exposure.total_cny),prior_hold_records=exposure.records,
        pricing=policy.pricing,relay_config=RelayConfig('auto',4096,INPUT_BOUND,True),provider_policy=policy,
        billing_proof=args.billing_proof,planning_preflight_only=args.planning_preflight_only)
    print(json.dumps(report.get('summary',{}),sort_keys=True,indent=2))
    good=(report['status']=='planning_preflight_passed' if args.planning_preflight_only else
          report.get('status')=='complete' and report.get('summary',{}).get('release_comparison_passed') is True)
    raise SystemExit(0 if good else 1)


if __name__=='__main__':
    main()
