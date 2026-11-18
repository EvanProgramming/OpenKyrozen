"""DeepSeek matched evaluation with CNY accounting and a default 15 CNY cap.

No credential files are consulted. Paid execution is explicit through this CLI;
importing this module and reconstructing old ledgers are offline operations.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import hashlib
from itertools import combinations
from pathlib import Path
import sqlite3
import secrets

from benchmarks.v3_matched import BudgetLedger, Pricing, _picormb
from benchmarks.v3_responses_relay import RelayConfig, StrictChatRelayConfig
from benchmarks.v3_panstar import PanstarPolicy, native_usage

DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_PRICING = Pricing(
    input_rate=Decimal("2"), output_rate=Decimal("8"),
    cache_read_rate=Decimal("0.04"), cache_write_rate=Decimal("2"),
    max_limit_cny=Decimal("15"),
    metadata_json=json.dumps({
        "currency": "CNY", "source_currency": "CNY", "provider": "DeepSeek official",
        "requested_model": "deepseek-flash", "served_model": "DeepSeek-V4.1-Flash-1017",
        "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "input_per_million": "2", "output_per_million": "8",
        "cache_read_per_million": "0.04", "cache_write_per_million": "2",
        "reservation": "peak rates, 1M input tokens and 4096 output tokens per call",
    }, sort_keys=True),
)
DEEPSEEK_RELAY_CONFIG = RelayConfig(
    model="deepseek-flash", max_output_tokens=4096,
    fixed_input_token_bound=1_000_000, serial_requests=True,
)
DEEPSEEK_STRICT_RELAY_CONFIG = StrictChatRelayConfig(**asdict(DEEPSEEK_RELAY_CONFIG))
PRO_MODEL = "deepseek-v4-pro"
MODEL_ROUTING_ARMS = frozenset({"routing-model-auto", "routing-model-simple"})
_PRO_RATES = {"input_per_million": "9", "output_per_million": "27",
              "cache_read_per_million": "0.30", "cache_write_per_million": "9"}
_PRO_PRICE = Pricing(*map(Decimal, _PRO_RATES.values()), max_limit_cny=None,
                     metadata_json=json.dumps({"currency": "CNY", **_PRO_RATES}))


def served_model_matches(model, served):
    versions = {"deepseek-flash": "DeepSeek-V4.1-Flash-1017", PRO_MODEL: "DeepSeek-V4-Pro-0813"}
    return model in versions and served in (model, versions[model])


_EMPTY_OBJECT = "is_empty"


def _closed_object(properties):
    if not properties:
        properties = {_EMPTY_OBJECT: {"type": "boolean", "enum": [True]}}
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


_WIRE_ARGUMENTS = "arguments"
_OMIT_FIELD = {"omit": True}
_OMIT_SCHEMA = _closed_object({"omit": {"type": "boolean", "enum": [True]}})
_OMIT_SCHEMA["description"] = 'Use {"omit":true} when this field does not apply.'
_MAP_ENTRIES = "key_value_entries"
_NULL_VALUE = "is_null"
_MAP_REF = {"$ref": "#/$def/map"}
_VALUE_REF = {"$ref": "#/$def/value"}
_STRICT_MAP = _closed_object({_MAP_ENTRIES: {"type": "array", "items": _closed_object({
    "key": {"type": "string"}, "value": _VALUE_REF})}})
_STRICT_MAP_DEFINITIONS = {
    "map": _STRICT_MAP,
    "value": {"anyOf": [
        *({"type": kind} for kind in ("string", "number", "boolean")),
        _closed_object({_NULL_VALUE: {"type": "boolean", "enum": [True]}}),
        {"type": "array", "items": _VALUE_REF}, _STRICT_MAP,
    ]},
}


def _flat_optional_fields(schema, *, root=False):
    properties = schema.get("properties", {})
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        return ()
    required = set(schema.get("required", ()))
    optional = tuple(name for name in properties if name not in required)
    # Prefer the canonical layout at the root when all optional subsets are allowed.
    compact_root = (root and required and schema.get("minProperties", 0) <= len(required)
                    and schema.get("maxProperties", float("inf")) >= len(properties))
    if len(optional) < 3 and "anyOf" not in schema and not compact_root:
        return ()
    for name in optional:
        field = properties[name]
        alternatives = field.get("anyOf", (field,)) if "type" not in field else (field,)
        # The omission marker must be disjoint from every allowed value of the field.
        if not alternatives or any(
            choice.get("type") not in {"string", "number", "integer", "boolean", "array"}
            and not (choice.get("type") == "object" and choice.get("additionalProperties") is False
                     and "omit" not in choice.get("properties", {}))
            for choice in alternatives
        ):
            return ()
    return optional


def strict_chat_schema(schema, *, remaining=None):
    """Project schemas to Strict Beta while leaving validation with the registry."""
    uses_maps = False
    remaining = [16_000] if remaining is None else remaining
    definitions = {}
    projected = {}

    def reference(item, depth):
        if depth > 32:
            raise ValueError("strict Chat tool schema exceeds its structural bound")
        if not isinstance(item, dict) or item.get("type") != "object" and "anyOf" not in item:
            return project(item, depth)
        identity = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if identity not in projected:
            name = f"schema_{len(projected)}"
            projected[identity] = name
            definitions[name] = project(item, depth)
        return {"$ref": f"#/$def/{projected[identity]}"}

    def consume(nodes):
        remaining[0] -= nodes
        if remaining[0] < 0:
            raise ValueError("strict Chat tool schemas exceed the projection bound")

    def project(item, depth):
        nonlocal uses_maps
        consume(1)
        if depth > 32 or not isinstance(item, dict):
            raise ValueError("strict Chat tool schema exceeds its structural bound")
        if set(item) & {"$ref", "$def", "$defs", "allOf", "oneOf"}:
            raise ValueError("strict Chat requires an inline canonical tool schema")
        original = item
        minimum = item.get("minProperties", 0)
        maximum = item.get("maxProperties", float("inf"))
        item = {key: value for key, value in item.items() if key not in {
            "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"}}
        if "anyOf" in item:
            branches = item["anyOf"]
            # The provider requires typed alternatives and rejects intersecting object declarations.
            if set(item) - {"anyOf", "description"}:
                props = item.get("properties")
                if (set(item) - {"anyOf", "description", "type", "properties", "required", "additionalProperties"}
                        or item.get("type") != "object" or item.get("additionalProperties") is not False
                        or not isinstance(props, dict)
                        or any(branch.get("type") != "object" or branch.get("additionalProperties") is not False
                               or not set(item.get("required", ())) <= set(branch.get("required", ()))
                               or any(props.get(key) != schema for key, schema in branch.get("properties", {}).items())
                               for branch in branches)):
                    raise ValueError("strict Chat union has unsupported intersecting constraints")
                optional = _flat_optional_fields(item)
                if optional:
                    return flat_object(item, optional, depth)
            alternatives = []
            for branch in branches:
                converted = project(branch, depth + 1)
                if converted.get("$ref") == _MAP_REF["$ref"]:
                    converted = _STRICT_MAP
                candidates = converted.get("anyOf", [converted])
                if any("type" not in candidate for candidate in candidates):
                    raise ValueError("strict Chat union has an untyped alternative")
                alternatives.extend(candidates)
            return {"anyOf": alternatives}
        if item.get("type") == "array":
            return {**item, "items": reference(item["items"], depth + 1)}
        if item.get("type") != "object":
            return item
        if item.get("additionalProperties") is True and not item.get("properties"):
            uses_maps = True
            return {**_MAP_REF, "description": "Represent object fields as named key/value entries."}
        if item.get("additionalProperties") is not False:
            raise ValueError("strict Chat Completions requires a declared object schema")
        optional = _flat_optional_fields(original, root=depth == 0)
        if optional:
            return flat_object(item, optional, depth)
        properties = {key: reference(value, depth + 1) for key, value in item.get("properties", {}).items()}
        required = item.get("required", [])
        optional = [name for name in properties if name not in required]
        if len(optional) > 8:
            raise ValueError("strict Chat object has too many optional fields")
        consume((1 + len(properties)) * (1 << len(optional)))
        first = max(0, minimum - len(required))
        last = min(len(optional), maximum - len(required))
        variants = [_closed_object({name: properties[name] for name in (*required, *extra)})
                    for count in range(first, last + 1) for extra in combinations(optional, count)]
        if not variants:
            raise ValueError("strict Chat object has no admissible field set")
        return variants[0] if len(variants) == 1 else {"anyOf": variants}

    def flat_object(item, optional, depth):
        properties = {}
        for name, value in item["properties"].items():
            field = reference(value, depth + 1)
            if name in optional:
                # A typed omission value cannot collide with the original field.
                if "$ref" in field:
                    field = definitions[field["$ref"].rsplit("/", 1)[-1]]
                field = {"anyOf": [*field.get("anyOf", [field]), _OMIT_SCHEMA]}
            properties[name] = field
        description = ("Supply the tool fields at the top level. Use the omission object for inapplicable fields."
                       if depth == 0 else item.get("description"))
        result = _closed_object(properties)
        if description:
            result["description"] = description
        return result

    result = project(schema, 0)
    if "anyOf" in result:
        result = _closed_object({_WIRE_ARGUMENTS: result})
    if uses_maps:
        definitions.update(_STRICT_MAP_DEFINITIONS)
    if definitions:
        result["$def"] = definitions
    return result


def decode_strict_chat_arguments(raw, schema):
    """Decode only declared map positions and carry the unmodified wire reply on replay."""
    from openkyrozen.providers.models import _unique_object
    from openkyrozen.tools.manifest import ToolRegistry

    def decode_map(value):
        if isinstance(value, list):
            return [decode_map(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {_NULL_VALUE} and value[_NULL_VALUE] is True:
            return None
        if set(value) != {_MAP_ENTRIES} or not isinstance(value[_MAP_ENTRIES], list):
            raise ValueError("invalid strict Chat map")
        result = {}
        for entry in value[_MAP_ENTRIES]:
            if (not isinstance(entry, dict) or set(entry) != {"key", "value"}
                    or not isinstance(entry["key"], str) or entry["key"] in result):
                raise ValueError("invalid or duplicate strict Chat map key")
            result[entry["key"]] = decode_map(entry["value"])
        return result

    def decode(value, contract, *, root=False):
        if contract.get("additionalProperties") is True and not contract.get("properties"):
            # Strict Beta can return canonical objects in referenced schemas.
            if isinstance(value, dict) and set(value) == {_MAP_ENTRIES} and isinstance(value[_MAP_ENTRIES], list):
                return decode_map(value)
            return value
        if isinstance(value, dict):
            optional = _flat_optional_fields(contract, root=root)
            if optional:
                value = {name: field for name, field in value.items()
                         if name not in optional or field != _OMIT_FIELD or field.get("omit") is not True}
        for branch in contract.get("anyOf", ()):
            try:
                result = decode(value, branch)
            except ValueError:
                continue
            if ToolRegistry._schema_error(branch, result, "$") is None:
                return result
        if contract.get("anyOf"):
            if ToolRegistry._schema_error(contract, value) is None:
                raise ValueError("invalid encoded fields inside canonical arguments")
            # Valid JSON still goes through the normal typed argument rejection.
            return value
        if isinstance(value, list) and "items" in contract:
            return [decode(item, contract["items"]) for item in value]
        if isinstance(value, dict) and "properties" in contract:
            if (not contract.get("required") and _EMPTY_OBJECT not in contract["properties"]
                    and set(value) == {_EMPTY_OBJECT} and value[_EMPTY_OBJECT] is True):
                return {}
            return {name: decode(item, contract["properties"].get(name, {})) for name, item in value.items()}
        return value

    value = json.loads(raw, object_pairs_hook=_unique_object)
    uses_envelope = "anyOf" in schema or set(schema.get("properties", {})) - set(schema.get("required", ()))
    if (uses_envelope and isinstance(value, dict) and set(value) == {_WIRE_ARGUMENTS}
            and isinstance(value[_WIRE_ARGUMENTS], dict) and ToolRegistry._schema_error(schema, value) is not None):
        value = value[_WIRE_ARGUMENTS]
    return json.dumps(decode(value, schema, root=True), ensure_ascii=False, allow_nan=False)


def _projected_bytes(value, sizes):
    if not isinstance(value, (dict, list)):
        return len(json.dumps(value, ensure_ascii=False).encode())
    identity = id(value)
    if identity not in sizes:
        if isinstance(value, dict):
            sizes[identity] = 2 + sum(_projected_bytes(key, sizes) + 2 + _projected_bytes(item, sizes)
                                      for key, item in value.items())
        else:
            sizes[identity] = 2 + sum(1 + _projected_bytes(item, sizes) for item in value)
    return sizes[identity]


def strict_chat_tools(tools):
    remaining = [16_000]
    result = [{"type": "function", "function": {**tool, "strict": True,
               "parameters": strict_chat_schema(tool["parameters"], remaining=remaining)}}
              for tool in tools or ()]
    if _projected_bytes(result, {}) > 999_488:
        raise ValueError("strict Chat tool schemas exceed the request bound")
    return result


def strict_chat_body(body):
    if (set(body) - {"model", "messages", "tools", "tool_choice", "max_tokens", "stream", "stream_options"}
            or body.get("model") != "deepseek-flash"
            or not isinstance(body.get("messages"), list) or not body["messages"]):
        raise ValueError("request is outside the pinned DeepSeek chat contract")
    if body.get("tool_choice") not in (None, "auto", "none"):
        raise ValueError("DeepSeek thinking mode does not support forced tool choices")
    for message in body["messages"]:
        if (not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}
                or (message.get("content") is not None and not isinstance(message["content"], str))
                or set(message) - {"role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content"}):
            raise ValueError("only text and native function history have an admitted price")
        if any(not isinstance(call, dict) or call.get("type") != "function"
               for call in message.get("tool_calls", [])):
            raise ValueError("only native function calls have an admitted price")
    for tool in body.get("tools", []):
        if (not isinstance(tool, dict) or set(tool) != {"type", "function"}
                or tool["type"] != "function" or not isinstance(tool["function"], dict)):
            raise ValueError("only native functions have an admitted price")
        function = tool["function"]
        if (set(function) != {"name", "description", "strict", "parameters"}
                or function["strict"] is not True or not isinstance(function["parameters"], dict)):
            raise ValueError("native functions must declare their strict wire schema")
    result = {**body, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    if result.get("stream") is True:
        result["stream_options"] = {"include_usage": True}
    return result


def deepseek_usage(response, *, strict_chat=False):
    raw = response.get("usage")
    if not isinstance(raw, dict) or "server_tool_use" in raw:
        return None
    if strict_chat:
        input_details = raw.get("prompt_tokens_details") or {}
        output_details = raw.get("completion_tokens_details") or {}
        if not isinstance(input_details, dict) or not isinstance(output_details, dict):
            return None
        cached = raw.get("prompt_cache_hit_tokens")
        if ("cached_tokens" in input_details and cached != input_details["cached_tokens"]
                or type(raw.get("prompt_cache_miss_tokens")) is not int or type(cached) is not int
                or type(raw.get("prompt_tokens")) is not int
                or raw["prompt_cache_miss_tokens"] != raw["prompt_tokens"] - cached):
            return None
        response = {**response, "usage": {
            "input_tokens": raw.get("prompt_tokens"), "output_tokens": raw.get("completion_tokens"),
            "total_tokens": raw.get("total_tokens"), "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": output_details,
        }}
    return native_usage(response)


class DeepSeekCampaignPricing(Pricing):
    billing_tier = "paid"

    def as_dict(self):
        return {**super().as_dict(), "model_routing_only": {PRO_MODEL: dict(_PRO_RATES)},
                "model_routing_financial_input_bound": 1048576}

    def for_metadata(self, metadata, *, input_tokens):
        if not metadata or metadata.get("model") == "deepseek-flash":
            return self
        if (metadata.get("model") != PRO_MODEL
                or metadata.get("arm") != "routing-model-auto"
                or metadata.get("max_output_tokens", 4096) != 4096):
            raise ValueError("the Pro quote is restricted to the model routing experiment")
        return _PRO_PRICE

    def row_cost(self, row):
        calls = row.get("cost_calls")
        if calls is None:
            return super().row_cost(row)
        totals = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"), 0)
        if not isinstance(calls, list) or not calls:
            raise ValueError("model routing cost requires physical receipts")
        amount = Decimal(0)
        for call in calls:
            usage = call.get("usage") or {}
            raw = deepseek_usage({"usage": usage.get("raw_usage"), "service_tier": usage.get("service_tier")})
            if (call.get("status") != "settled" or raw is None
                    or any(usage.get(key) != value for key, value in raw.items())
                    or not served_model_matches(call.get("model"), usage.get("served_model"))):
                raise ValueError("invalid official DeepSeek usage or served model")
            price = self.for_metadata(call, input_tokens=usage["input_tokens"])
            cost = price.cost(*(usage[key] for key in totals))
            if Decimal(call["actual_rmb"]) != cost:
                raise ValueError("model routing physical cost differs from the quote")
            amount += cost
            for key in totals:
                totals[key] += usage[key]
        if any(row[key] != total for key, total in totals.items()):
            raise ValueError("model routing totals differ from recorded calls")
        return amount


DEEPSEEK_UNLIMITED_PRICING = DeepSeekCampaignPricing(**{
    **asdict(DEEPSEEK_PRICING), "max_limit_cny": None,
})


class DeepSeekRoutingPolicy(PanstarPolicy):
    """Only the separate model-routing supplement may expose the Pro slot."""
    endpoint = DEEPSEEK_ENDPOINT
    paid_fx = None

    def __init__(self, *, price_date=None):
        from datetime import datetime, timezone
        self.price_date = price_date or datetime.now(timezone.utc).date().isoformat()
        self.role_models = {"simple": DEEPSEEK_RELAY_CONFIG.model, "complex": PRO_MODEL}
        self.pricing = DEEPSEEK_UNLIMITED_PRICING
        self.billing_record = self.billing_proof_sha256 = None

    def identity(self):
        return {"provider": self.provider, "service": "DeepSeek official", "protocol": "openai-responses",
                "model_main": "auto", "role_models": dict(self.role_models), "price_date": self.price_date,
                "pricing_sha256": self.pricing.sha256, "phase": "model_routing"}

    def served_identity(self, model, served_model):
        return served_model_matches(model, served_model)

    def validate_body(self, body):
        return self._validate_text_body(body, self.role_models.values(), reasoning_text=True)

    def response_usage(self, model, response):
        if deepseek_usage(response) is None:
            return None
        return super().response_usage(model, response)

    def request_metadata(self, model, arm):
        if arm not in MODEL_ROUTING_ARMS or (arm == "routing-model-simple" and model == PRO_MODEL):
            raise ValueError("model routing request is outside its assigned model variant")
        return {**self.metadata(model), "arm": arm}

    def accepts_call(self, call):
        try:
            self.request_metadata(call.get("model"), call.get("arm"))
        except ValueError:
            return False
        usage = call.get("usage")
        if isinstance(usage, dict) and deepseek_usage(
                {"usage": usage.get("raw_usage"), "service_tier": usage.get("service_tier")}) is None:
            return False
        return super().accepts_call(call)

    def check_budget(self, budget):
        if budget.pricing != self.pricing or budget.limit_picormb is not None or not budget.budget_ok:
            raise ValueError("DeepSeek model routing requires its intact inherited CNY ledger")
# The previous campaign used the same CNY prices under this retired model alias.
_LEGACY_PRICING_SHA256 = "70cf92ad796b1061598eb1d5ac16aa19d473299758bcfdde86f789b2d40aabc4"


@dataclass(frozen=True)
class PriorExposure:
    settled_cny: Decimal
    held_cny: Decimal
    attempt_count: int
    records: tuple[str, ...] = ()

    @property
    def total_cny(self) -> Decimal:
        return self.settled_cny + self.held_cny


def read_prior_exposure(paths: list[Path] | tuple[Path, ...]) -> PriorExposure:
    """Reconstruct old costs read-only; duplicate journal attempts count once."""
    if not paths:
        raise ValueError("at least one original prior campaign ledger is required")
    seen: dict[str, str] = {}
    amounts: dict[str, tuple[int, int]] = {}
    sources: list[tuple[set[str], list[set[str]]]] = []
    opaque_anchors: list[set[str]] = []
    from benchmarks.v3_gemini import GeminiPricing, INPUT_BOUND
    from benchmarks.v3_panstar import PanstarPricing
    native_prices = {price.sha256: price for price in (GeminiPricing(), GeminiPricing(billing_tier="free"),
        GeminiPricing(max_limit_cny=None), GeminiPricing(billing_tier="free", max_limit_cny=None),
        PanstarPricing(), PanstarPricing(terra=True), DEEPSEEK_UNLIMITED_PRICING)}

    def add(record: dict) -> tuple[int, int]:
        attempt_id = record.get("id")
        reserved, actual = record.get("reserved"), record.get("actual")
        status, usage = record.get("status"), record.get("usage")
        if (not isinstance(attempt_id, str) or not attempt_id or type(reserved) is not int
                or reserved < 0 or status not in {"settled", "reserved", "unknown"}):
            raise ValueError("invalid or overrun attempt in prior campaign ledger")
        normalized_record = {"id": attempt_id, "reserved": reserved, "actual": actual,
                             "status": status, "usage": usage}
        native_price = native_prices.get(record.get("pricing_sha256"))
        metadata = record.get("metadata")
        if "pricing_sha256" in record:
            if (native_price is None or not isinstance(metadata, dict)
                    or metadata.get("pricing_sha256") != record["pricing_sha256"]):
                raise ValueError("unknown prior native model/rate provenance")
            quote = native_price.for_metadata(metadata, input_tokens=INPUT_BOUND).cost(INPUT_BOUND, 4096)
            if reserved != _picormb(quote):
                raise ValueError("prior native reservation differs from its immutable model quote")
            normalized_record.update(pricing_sha256=record["pricing_sha256"], metadata=metadata)
        normalized = json.dumps(normalized_record, sort_keys=True)
        if attempt_id in seen:
            if seen[attempt_id] != normalized:
                raise ValueError("conflicting copies of a prior campaign attempt")
            return amounts[attempt_id]
        if status == "settled":
            if type(actual) is not int or actual < 0 or actual > reserved or not isinstance(usage, dict):
                raise ValueError("settled prior attempt lacks valid usage accounting")
            required = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
            if any(field not in usage for field in required):
                raise ValueError("settled prior attempt has incomplete usage accounting")
            price = (native_price.for_metadata(metadata, input_tokens=usage["input_tokens"])
                     if native_price is not None else DEEPSEEK_PRICING)
            reconstructed = _picormb(price.cost(*(usage[field] for field in required)))
            if native_price is not None:
                native_price.row_cost(dict(metadata, cost_calls=[dict(metadata, usage=usage, status='settled',
                    actual_rmb=str(Decimal(actual)/Decimal(10**12)))], **{key:usage[key] for key in required}))
            if actual != reconstructed:
                raise ValueError("prior attempt charge differs from reconstructed CNY usage")
            amount = (reconstructed, 0)
        else:
            if actual is not None or usage is not None:
                raise ValueError("unsettled prior attempts must retain their full reservation")
            amount = (0, reserved)
        seen[attempt_id], amounts[attempt_id] = normalized, amount
        return amount

    try:
        for path in paths:
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                config = db.execute("SELECT currency_code,pricing_sha256 FROM budget_config WHERE id=1").fetchone()
                if config not in {("CNY", _LEGACY_PRICING_SHA256), ("CNY", DEEPSEEK_PRICING.sha256),
                                   *(("CNY", digest) for digest in native_prices)}:
                    raise ValueError("prior ledger does not pin the approved DeepSeek CNY prices")
                attempts = db.execute(
                    "SELECT id,reserved_picormb,actual_picormb,status,usage_json,metadata_json FROM attempts"
                ).fetchall()
            source_ids: set[str] = set()
            manifests: list[set[str]] = []
            direct_opaque = False
            for attempt_id, reserved, actual, status, usage_json, metadata_json in attempts:
                metadata = json.loads(metadata_json) if metadata_json else {}
                if attempt_id == "prior-campaign-reservations" and "prior_attempts" in metadata:
                    manifest = metadata["prior_attempts"]
                    if (status != "unknown" or actual is not None or usage_json is not None
                            or not isinstance(manifest, list) or not manifest
                            or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
                            != metadata.get("prior_attempts_sha256")):
                        raise ValueError("carried prior campaign provenance is invalid")
                    represented = [json.loads(item) for item in manifest]
                    if len({item["id"] for item in represented}) != len(represented):
                        raise ValueError("carried prior campaign contains duplicate attempts")
                    manifest_ids = {item["id"] for item in represented}
                    manifests.append(manifest_ids)
                    source_ids.update(manifest_ids)
                    if "prior-campaign-reservations" in manifest_ids:
                        opaque_anchors.append(manifest_ids)
                    reconstructed = sum(sum(add(item)) for item in represented)
                    if reconstructed != reserved:
                        raise ValueError("carried prior campaign hold differs from its underlying attempts")
                    continue
                item = {"id": attempt_id, "reserved": reserved, "actual": actual,
                        "status": status, "usage": json.loads(usage_json) if usage_json else None}
                if config[1] == DEEPSEEK_UNLIMITED_PRICING.sha256 and "pricing_sha256" not in metadata:
                    if (attempt_id != "prior-campaign-reservations" and
                            (metadata.get("model") != "deepseek-flash"
                             or metadata.get("arm") in MODEL_ROUTING_ARMS
                             or reserved != _picormb(DEEPSEEK_PRICING.cost(1000000, 4096)))):
                        raise ValueError("invalid fixed Flash attempt provenance")
                elif config[1] in native_prices and attempt_id != "prior-campaign-reservations":
                    item.update(pricing_sha256=config[1], metadata=metadata)
                add(item)
                source_ids.add(attempt_id)
                direct_opaque = direct_opaque or attempt_id == "prior-campaign-reservations"
            sources.append((source_ids, manifests))
            if direct_opaque:
                opaque_anchors.append(source_ids)
    except (sqlite3.Error, OSError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot reconstruct the required prior campaign ledger") from exc
    # Opaque holds can accompany copies or proven descendants, not unrelated
    # source histories whose costs may already be included in that aggregate.
    for anchor in opaque_anchors:
        if any(not source <= anchor and not any(anchor <= manifest for manifest in manifests)
               for source, manifests in sources):
            raise ValueError("ambiguous opaque prior campaign aggregate requires original provenance")
    settled = sum(amount[0] for amount in amounts.values())
    held = sum(amount[1] for amount in amounts.values())
    return PriorExposure(Decimal(settled) / 1_000_000_000_000,
                         Decimal(held) / 1_000_000_000_000, len(seen),
                         tuple(seen[key] for key in sorted(seen)))


def create_budget(path: Path, *, prior_ledgers: list[Path] | tuple[Path, ...],
                  limit_cny: str | Decimal = "15") -> BudgetLedger:
    exposure = read_prior_exposure(prior_ledgers)
    return BudgetLedger(path, limit_rmb=limit_cny, prior_hold_rmb=exposure.total_cny,
                        pricing=DEEPSEEK_PRICING, prior_hold_records=exposure.records)


PLANNING_PREFLIGHT_CONTENT = "planning-preflight-ok\n"


def planning_preflight_task() -> dict:
    return {"description": "Read and verify proof.txt", "acceptance": [{
        "criterion": "The complete proof file has the expected marker",
        "verification": {"action": "read_file", "arguments": {"path": "proof.txt"},
                         "expect": {"equals": PLANNING_PREFLIGHT_CONTENT}},
    }]}


def planning_preflight_fixture(candidate: str) -> dict:
    creation = {"expected_revision": 0,
        "operations": [{"op": "add", "ref": "proof", "task": planning_preflight_task()},
                       {"op": "start", "task": "proof"}]}
    return {"fixture_id": candidate, "category": "planning_preflight",
        "prompt": "This is a planning preflight, separate from the formal evaluation. Do not write files or run commands.\n"
        "1. Create and explicitly start the Working Plan with update_plan using this object: "
        + json.dumps(creation, ensure_ascii=False) + "\n"
        "2. Call read_file with {\"path\":\"proof.txt\"}. The explicitly selected task owns this native receipt; "
        "do not add unsupported task_id fields to read_file. The complete content must satisfy the typed equals predicate. "
        "The verified receipt completes the task.\n"
        "3. Confirm using update_plan with the returned actual plan_id, its unchanged current expected_revision, "
        "and operations [{\"op\":\"suggest_complete\",\"task\":\"<returned actual task ID>\"}]. "
        "The pinned current Working Plan state retains these IDs after the read completes it; "
        "if an earlier result is archived, the permitted read_tool_artifact reader can retrieve it. "
        "After the actual completed plan is confirmed, return the file marker as your final answer.",
        "files": {"proof.txt": PLANNING_PREFLIGHT_CONTENT},
        "verifier": {"kind": "answer_contains", "all": [PLANNING_PREFLIGHT_CONTENT.strip()]},
        "plan_tasks": [], "plan_scenario": None, "turns": None,
        "capabilities": ["read", "write"], "context_window_tokens": 1_000_000,
        "output_reserve_tokens": 4096}


def _preflight_arguments(receipt: dict) -> dict:
    arguments = receipt.get("arguments", receipt.get("args"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def planning_preflight_semantics(worker: dict) -> bool:
    """Require actual ordered native receipts and exact durable task identity."""
    if (not isinstance(worker, dict) or not isinstance(worker.get("answer"), str)
            or PLANNING_PREFLIGHT_CONTENT.strip() not in worker["answer"]
            or worker.get("protocol_recovery") != 0 or worker.get("duplicate_calls") != 0
            or worker.get("tasks") != ["succeeded"]):
        return False
    plan = worker.get("working_plan")
    if not isinstance(plan, dict):
        return False
    tasks = plan.get("tasks", [])
    if (plan.get("status") != "completed" or not isinstance(tasks, list) or len(tasks) != 1
            or not isinstance(tasks[0], dict) or tasks[0].get("status") != "succeeded"):
        return False
    task_id, plan_id = tasks[0].get("id"), plan.get("plan_id")
    if not task_id or not plan_id:
        return False
    records = worker.get("task_records") or []
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict)
            or records[0].get("id") != task_id or records[0].get("status") != "succeeded"
            or records[0].get("description") != planning_preflight_task()["description"]
            or records[0].get("dependencies") != [] or not isinstance(records[0].get("checkpoint"), dict)
            or not isinstance(records[0].get("evidence"), list)
            or any(not isinstance(item, dict) for item in records[0]["evidence"])):
        return False
    criterion = planning_preflight_task()["acceptance"][0]
    criterion_id = "read-ac-" + hashlib.sha256(json.dumps(criterion, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:12]
    expected = {"id": criterion_id, "criterion": criterion["criterion"], "action": "read_file",
        "args": '{"path":"proof.txt"}', "expect": {"equals": PLANNING_PREFLIGHT_CONTENT}}
    if (records[0].get("acceptance") != [expected]
            or (records[0].get("checkpoint") or {}).get("native_execution_selected") is not True):
        return False
    allowed = {"update_plan", "read_file", "get_file_hash", "list_dir", "list_tree",
               "find_files", "search_files", "discover_tools", "read_tool_artifact"}
    observed = worker.get("tool_receipts")
    if not isinstance(observed, list) or any(not isinstance(item, dict) for item in observed):
        return False
    receipts = [item.get("evidence") for item in observed]
    if not receipts or any(not isinstance(receipt, dict) or receipt.get("action") not in allowed
            or receipt.get("call_id_source") != "native" or receipt.get("authorized") is not True
            or receipt.get("success") is not True
            or not isinstance(receipt.get("verification"), dict)
            or receipt.get("side_effects") not in {"internal", "read_only", "read", "none"}
            for receipt in receipts):
        return False
    for field in ("call_id", "receipt_id"):
        values = [receipt.get(field) for receipt in receipts]
        if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            return False
    stage = 0
    scope = None
    turn_id = None
    for receipt in receipts:
        arguments = _preflight_arguments(receipt)
        result = receipt.get("result_data") or {}
        if not isinstance(result, dict):
            return False
        current_scope = tuple(receipt.get(field) for field in ("user_id", "workspace_id", "session_id"))
        if not all(isinstance(value, str) and value for value in current_scope):
            return False
        scope = scope or current_scope
        if current_scope != scope:
            return False
        if receipt["action"] == "update_plan":
            operations = arguments.get("operations")
            verification = receipt["verification"]
            observed = verification.get("observed")
            if (verification.get("status") != "verified" or not isinstance(observed, dict)
                    or observed.get("plan_id") != plan_id or observed.get("revision") != 1
                    or not isinstance(observed.get("turn_id"), str) or not observed["turn_id"]):
                return False
            turn_id = turn_id or observed["turn_id"]
            if observed["turn_id"] != turn_id:
                return False
            if (stage == 0 and isinstance(operations, list) and len(operations) == 2
                    and isinstance(operations[0], dict) and operations[0].get("op") == "add"
                    and operations[0].get("ref") == "proof"
                    and operations[1] == {"op": "start", "task": "proof"}):
                if (arguments.get("expected_revision") != 0 or result.get("plan_id") != plan_id
                        or result.get("task_ids") != [task_id] or result.get("status") != "active"
                        or verification.get("method") != "working_plan_commit"):
                    return False
                stage = 1
            elif stage == 2 and operations == [{"op": "suggest_complete", "task": task_id}]:
                if (arguments.get("plan_id") != plan_id or arguments.get("expected_revision") != 1
                        or result.get("plan_id") != plan_id or result.get("status") != "completed"
                        or result.get("revision") != 1 or plan.get("revision") != 1
                        or (receipt.get("verification") or {}).get("method") != "working_plan_confirmation"):
                    return False
                stage = 3
            else:
                return False
        elif receipt["action"] == "read_file":
            if (stage != 1 or arguments != {"path": "proof.txt"} or receipt.get("task_id") != task_id
                    or receipt.get("plan_id") != plan_id or result.get("content") != PLANNING_PREFLIGHT_CONTENT
                    or result.get("sha256") != hashlib.sha256(PLANNING_PREFLIGHT_CONTENT.encode()).hexdigest()
                    or result.get("truncated") is not False or not receipt.get("acceptance")
                    or (receipt.get("verification") or {}).get("status") not in {"verified", "not_applicable"}):
                return False
            if not any(item.get("receipt_id") == receipt.get("receipt_id") and item.get("success") is True
                       and item.get("task_id") == task_id
                       and item.get("acceptance_ids") == [criterion_id]
                       for item in records[0].get("evidence", [])):
                return False
            stage = 2
    events = worker.get("events")
    if not isinstance(events, list):
        return False
    snapshots = [event.get("working_plan") for event in events if isinstance(event, dict)]
    return stage == 3 and any(isinstance(snapshot, dict) and snapshot.get("plan_id") == plan_id
        and snapshot.get("revision") == 1 and snapshot.get("status") == "active"
        and isinstance(snapshot.get("tasks"), list)
        and any(task.get("id") == task_id and task.get("status") == "running"
                for task in snapshot["tasks"] if isinstance(task, dict)) for snapshot in snapshots)


def _planning_preflight_identity(candidate: str) -> str:
    return hashlib.sha256(json.dumps(planning_preflight_fixture(candidate), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_planning_preflight(output: Path, record: dict, calls: list[dict],
                                candidate: str, identity: dict, provider_policy=None) -> None:
    from benchmarks.v3_matched_live import _validate_worker_evidence, _safety_failure_status
    label = ("planning_preflight", candidate, -1)
    from benchmarks.v3_matched_live import _fixture_prompt
    prompt_sha256 = hashlib.sha256(_fixture_prompt(planning_preflight_fixture(candidate)).encode()).hexdigest()
    phase_calls = [call for call in calls if call.get("arm") == label[0]]
    if (not isinstance(record, dict) or record.get("status") != "passed"
            or record.get("fixture_sha256") != _planning_preflight_identity(candidate)
            or record.get("prompt_sha256") != prompt_sha256
            or not isinstance(record.get("attempt_ids"), list)
            or not 1 <= len(record["attempt_ids"]) <= 6
            or any(not isinstance(item, str) or not item for item in record["attempt_ids"])
            or len(set(record["attempt_ids"])) != len(record["attempt_ids"])
            or set(record["attempt_ids"]) != {call.get("attempt_id") for call in phase_calls}
            or len(phase_calls) != len(record["attempt_ids"])):
        raise ValueError("planning preflight checkpoint or journal is incomplete")
    if any((call.get("arm"), call.get("fixture_id"), call.get("repeat")) != label
            or (not provider_policy.accepts_call(call) if provider_policy is not None else
                call.get("model") != DEEPSEEK_RELAY_CONFIG.model or call.get("max_output_tokens") != 4096
                or call.get("reserved_rmb") != "2.032768")
            or call.get("status") != "settled" or not isinstance(call.get("usage"), dict)
            for call in phase_calls):
        raise ValueError("planning preflight calls lack exact settled CNY lineage")
    usage_fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    for call in phase_calls:
        usage = call["usage"]
        if (any(type(usage.get(key)) is not int or usage[key] < 0 for key in usage_fields)
                or usage["cache_read_tokens"] + usage["cache_write_tokens"] > usage["input_tokens"]
                or usage["input_tokens"] > 1_000_000 or usage["output_tokens"] > 4096):
            raise ValueError("planning preflight usage is incomplete or invalid")
        try:
            actual = Decimal(call.get("actual_rmb"))
            if provider_policy is not None:
                from benchmarks.v3_gemini import INPUT_BOUND
                price = provider_policy.pricing.for_metadata(call, input_tokens=usage["input_tokens"])
                reserve = provider_policy.pricing.for_metadata(call, input_tokens=INPUT_BOUND).cost(INPUT_BOUND, 4096)
                if Decimal(call["reserved_rmb"]) != reserve:
                    raise ValueError("planning preflight reservation differs from its native model")
            else:
                price = DEEPSEEK_PRICING
            expected = price.cost(*(usage[key] for key in usage_fields))
            if not actual.is_finite() or actual < 0 or actual != expected:
                raise ValueError("planning preflight actual charge differs from its pinned usage")
        except (InvalidOperation, TypeError):
            raise ValueError("planning preflight actual charge is invalid") from None
    row = record.get("row")
    if (not isinstance(row, dict) or (row.get("arm"), row.get("fixture_id"), row.get("repeat")) != label
            or row.get("worker_exit_code") != 0 or row.get("worker_result_complete") is not True
            or row.get("usage_complete") is not True or row.get("verified_success") is not True
            or row.get("requests") != len(phase_calls) or _safety_failure_status(row) is not None):
        raise ValueError("planning preflight worker did not complete safely")
    totals = {key: sum(call["usage"][key] for call in phase_calls) for key in usage_fields}
    totals.update(attempts=len(phase_calls), requests=len(phase_calls))
    if any(type(row.get(key)) is not int or row[key] != value for key, value in totals.items()):
        raise ValueError("planning preflight row differs from its exact journal totals")
    if provider_policy is not None:
        costs = row.get('cost_calls')
        by_id = {call['attempt_id']:call for call in phase_calls}
        if (not isinstance(costs,list) or len(costs)!=len(by_id)
                or {call.get('attempt_id') for call in costs}!=set(by_id)
                or any(call.get('usage')!=by_id[call['attempt_id']].get('usage')
                       or any(call.get(key)!=by_id[call['attempt_id']].get(key)
                              for key in ('model','billing_tier','price_date','pricing_sha256')) for call in costs)):
            raise ValueError('planning preflight native call lineage changed')
        provider_policy.pricing.row_cost(row)
    worker = _validate_worker_evidence(output, row, candidate, identity, return_worker=True)
    conversation = worker.get("conversation_records") if isinstance(worker, dict) else None
    if (not isinstance(conversation, list) or len(conversation) != 1 or not isinstance(conversation[0], dict)
            or conversation[0].get("turn") != 0 or conversation[0].get("prompt_sha256") != prompt_sha256
            or conversation[0].get("completed") is not True or not planning_preflight_semantics(worker)):
        raise ValueError("planning preflight lacks actual verified completion")


def run_planning_preflight(**options) -> dict:
    from benchmarks.v3_matched_live import _execute_cell, _fixture_prompt
    candidate, relay = options["candidate"], options["relay"]
    provider_policy = getattr(relay, "policy", None)
    record = {"status": "running", "fixture_sha256": _planning_preflight_identity(candidate),
        "prompt_sha256": hashlib.sha256(_fixture_prompt(planning_preflight_fixture(candidate)).encode()).hexdigest()}
    # Keep the finite label for this relay's lifetime: queued requests may
    # already be authorized when the parent advances to a different phase.
    if relay.planning_preflight_closed:
        raise ValueError("planning preflight admission is already closed")
    relay.planning_preflight_label = ("planning_preflight", candidate, -1)
    try:
        record["row"] = _execute_cell(**options, repeat=-1, fixture=planning_preflight_fixture(candidate),
                                     arm="planning_preflight")
    except BaseException:
        # Interrupted work cannot succeed; close admission without delaying cancellation.
        relay.planning_preflight_closed = True
        raise
    with relay._upstream_lock:
        # Drain reserve/append admission before closing and validating a stable
        # snapshot. A still-unsettled stream fails validation conservatively.
        relay.planning_preflight_closed = True
        calls = json.loads(json.dumps(relay.rows))
        record["attempt_ids"] = [call["attempt_id"] for call in calls
                                 if call.get("arm") == "planning_preflight"]
        record["status"] = "passed"
        try:
            validate_planning_preflight(options["output"], record, calls, candidate, options["identity"], provider_policy)
        except ValueError:
            record["status"] = "failed"
    return record


def run_smoke(relay, candidate: str) -> bool:
    """Verify typed tool continuation through the same metered campaign relay."""
    if getattr(relay, "smoke_verified_candidate", None) == candidate:
        return True
    if getattr(getattr(relay, "config", None), "strict_chat", False) is True:
        return _run_strict_chat_smoke(relay, candidate)
    import requests
    probe = secrets.token_hex(16)
    tools = [{"type": "function", "name": "echo_probe", "description": "Echo a benchmark probe value.",
              "parameters": {"type": "object", "properties": {
                  "value": {"type": "string", "enum": [probe]}},
                  "required": ["value"], "additionalProperties": False}, "strict": False}]
    history = [{"role": "user", "content":
                "Call echo_probe with the allowed value, then repeat the returned value exactly."}]
    relay.label = ("smoke", candidate, -1)
    attempt_ids = []

    def request(body: dict) -> dict | None:
        before = len(relay.rows)
        response = requests.post(relay.url + "/responses", json=body,
                                 headers={"Authorization": "Bearer " + relay.client_key}, timeout=190)
        if (response.status_code != 200 or len(relay.rows) != before + 1
                or relay.rows[-1].get("status") != "settled"
                or not isinstance(relay.rows[-1].get("usage"), dict) or not relay.budget.budget_ok):
            if not relay.budget_blocked and not relay.provider_error:
                relay.provider_error = "smoke_incomplete_usage"
            return None
        attempt_ids.append(relay.rows[-1]["attempt_id"])
        result = response.json()
        return result if isinstance(result, dict) else None

    try:
        first = request({"input": history, "tools": tools,
                         "tool_choice": "auto",
                         "parallel_tool_calls": False, "stream": False})
        if first is None:
            return False
        output = first.get("output")
        calls = [item for item in output if isinstance(item, dict)
                 and item.get("type") == "function_call"] if isinstance(output, list) else []
        if len(calls) != 1:
            relay.provider_error = "smoke_invalid_tool_call"
            return False
        call = calls[0]
        if (call.get("name") != "echo_probe" or not isinstance(call.get("call_id"), str)
                or not call["call_id"].strip() or not isinstance(call.get("arguments"), str)):
            relay.provider_error = "smoke_invalid_tool_call"
            return False
        arguments = json.loads(call["arguments"])
        if arguments != {"value": probe}:
            relay.provider_error = "smoke_invalid_tool_arguments"
            return False
        final = request({"input": history + output + [{"type": "function_call_output",
                         "call_id": call["call_id"], "output": json.dumps({"value": probe})}],
                         "tools": tools, "tool_choice": "none", "stream": False})
        if final is None:
            return False
        final_output = final.get("output")
        messages = final_output if isinstance(final_output, list) else []
        text = "".join(content.get("text", "") for item in messages if isinstance(item, dict)
                       and item.get("type") == "message" and item.get("role") == "assistant"
                       and isinstance(item.get("content"), list)
                       for content in item["content"] if isinstance(content, dict)
                       and content.get("type") == "output_text" and isinstance(content.get("text"), str))
        if (probe not in text or any(isinstance(item, dict) and item.get("type") == "function_call"
                                    for item in messages)):
            relay.provider_error = "smoke_invalid_continuation"
            return False
    except (requests.RequestException, ValueError):
        if not relay.provider_error:
            relay.provider_error = "smoke_invalid_response"
        return False
    relay.smoke_verified_candidate = candidate
    relay.smoke_verified_attempt_ids = tuple(attempt_ids)
    return True


def _run_strict_chat_smoke(relay, candidate):
    from openkyrozen.providers.config import ProviderConfig
    from openkyrozen.providers.errors import ProviderError
    from openkyrozen.providers.models import ToolReply
    from openkyrozen.providers.openai import OpenAICompatProvider

    probe = secrets.token_hex(16)
    tool = {"name": "echo_probe", "description": "Echo the permitted value.",
            "parameters": {"type": "object", "properties": {
                "value": {"type": "string", "enum": [probe]}},
                "required": ["value"], "additionalProperties": False}}
    model = relay.config.model
    provider = OpenAICompatProvider(ProviderConfig(provider="deepseek", base_url=relay.url,
        api_key=relay.client_key, model_simple=model, model_complex=model))
    provider._tool_wire_decoder = decode_strict_chat_arguments
    provider._tool_wire_projector = strict_chat_tools
    messages = [{"role": "user", "content":
                 "Call echo_probe with the permitted value, then repeat the returned value exactly."}]
    relay.label = ("smoke", candidate, -1)
    before = len(relay.rows)
    try:
        first = provider.chat_response(messages, tools=[tool])
        if (len(first.tool_calls) != 1 or first.tool_calls[0].name != "echo_probe"
                or first.tool_calls[0].arguments != {"value": probe}):
            relay.provider_error = "smoke_invalid_tool_arguments"
            return False
        history = [*messages, first, ToolReply(first.tool_calls[0].id, "echo_probe", {"value": probe}),
                   {"role": "system", "content": "Return only the tool's value now."}]
        final = provider.chat_response(history, tools=[tool])
        if final.text.strip() != probe or final.tool_calls:
            relay.provider_error = "smoke_invalid_continuation"
            return False
    except (ProviderError, ValueError):
        relay.provider_error = relay.provider_error or "smoke_invalid_response"
        return False
    finally:
        provider._client.close()
    calls = relay.rows[before:]
    if (not relay.budget.budget_ok or len(calls) != 2
            or any(call["status"] != "settled" for call in calls)):
        relay.provider_error = relay.provider_error or "smoke_incomplete_usage"
        return False
    relay.smoke_verified_candidate = candidate
    relay.smoke_verified_attempt_ids = tuple(call["attempt_id"] for call in calls)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-db", type=Path, required=True)
    parser.add_argument("--prior-ledger", type=Path, action="append", required=True)
    parser.add_argument("--limit-cny", default="15",
                        help="Cumulative cap in CNY up to 15, or explicit 'unlimited' with the full ledger.")
    parser.add_argument("--planning-preflight-only", action="store_true",
                        help="Run both protected preflights, including bug_fix-1 (up to 16 requests), and stop before the formal cohort.")
    args = parser.parse_args()
    from benchmarks.v3_matched_live import _source_snapshot, run
    from scripts.publish_v3_preview import _verify_candidate_signature
    if _source_snapshot(args.source.resolve()) != args.candidate_sha:
        raise ValueError("source does not match the configured frozen candidate")
    _verify_candidate_signature(args.source.resolve(), args.candidate_sha)
    exposure = read_prior_exposure(args.prior_ledger)
    pricing = DEEPSEEK_PRICING
    if args.limit_cny == "unlimited":
        pricing = DEEPSEEK_UNLIMITED_PRICING
    else:
        try:
            limit = Decimal(args.limit_cny)
        except InvalidOperation:
            raise ValueError("invalid cumulative DeepSeek cap") from None
        if not limit.is_finite() or not 0 < limit <= 15 or exposure.total_cny > limit:
            raise ValueError("the cumulative capped DeepSeek campaign must fit within 15 CNY")
    report = run(source=args.source, repo=args.repo, output=args.output, budget_path=args.budget_db,
                 limit_cny=args.limit_cny, prior_hold_cny=str(exposure.total_cny),
                 prior_hold_records=exposure.records,
                 pricing=pricing, relay_config=DEEPSEEK_STRICT_RELAY_CONFIG,
                 endpoint=DEEPSEEK_ENDPOINT, planning_preflight_only=args.planning_preflight_only)
    if args.planning_preflight_only:
        print(json.dumps({"status": report["status"], "planning_preflight": report.get("planning_preflight"),
                          "bug_fix_preflight": report.get("bug_fix_preflight"),
                          "budget": report["budget"]}, indent=2, sort_keys=True))
        raise SystemExit(0 if report["status"] == "planning_preflight_passed" else 1)
    summary = report["summary"]
    delegation = report.get("delegation_comparison") or {}
    print(json.dumps({**summary, "status": report.get("status"),
                      "delegation_comparison": delegation}, indent=2, sort_keys=True))
    raise SystemExit(0 if report.get("status") == "complete" and summary["complete"]
                     and summary["accounting_complete"] and summary["comparison_thresholds_met"]
                     and delegation.get("complete") is True else 1)


if __name__ == "__main__":
    main()
