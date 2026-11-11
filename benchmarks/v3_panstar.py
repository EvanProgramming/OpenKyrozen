"""Benchmark-only Panstar Responses Auto policy; imports never read credentials."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from urllib.parse import urlsplit

from benchmarks.v3_gemini import GeminiPolicy, INPUT_BOUND, OPERATIONAL_INPUT_BOUND, OUTPUT_BOUND
from benchmarks.v3_matched import Pricing
from benchmarks.v3_responses_relay import ResponsesRelay, RelayConfig

OFFICIAL_ENDPOINT = 'https://ai.panstar.ai/v1'
MODELS = ('gpt-6-sol', 'gpt-6.1-sol')
PRICING_SOURCE_SHA256 = '475171fad81ad6a7805bf44ab5826ea792e0f7bfd91514daca0375367dfe80e4'
MODEL_METADATA_SHA256 = '985c02e068f78537e7834abd5fa5dd3af374b228d58c50b48bfc2556172a0368'


@dataclass(frozen=True)
class PanstarPrice(Pricing):
    cache_write_available: bool = False

    def cost(self, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0):
        if cache_write_tokens and not self.cache_write_available:
            raise ValueError('short-context cache writes have no published Panstar price')
        return super().cost(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)


@dataclass(frozen=True)
class PanstarPricing:
    billing_tier: str = 'paid'
    max_limit_cny: None = None

    def __post_init__(self):
        if self.billing_tier != 'paid' or self.max_limit_cny is not None:
            raise ValueError('Panstar native Auto requires the explicit owner-authorized paid upper policy')

    def as_dict(self):
        return {'currency': 'CNY', 'provider': 'PanstarCloud Responses', 'source_currency': 'USD',
                'billing_tier': 'paid', 'max_limit_cny': None, 'conservative_cny_per_usd': '8',
                'cost_basis': 'dated account quote at conservative FX; not an invoice',
                'pricing_source_sha256': PRICING_SOURCE_SHA256, 'model_metadata_sha256': MODEL_METADATA_SHA256,
                'long_context_threshold': 272000, 'long_context_entire_request': True,
                'financial_input_reservation': INPUT_BOUND, 'operational_input_token_limit': OPERATIONAL_INPUT_BOUND,
                'reservation_input_usd_per_million': '0.33',
                'models': {model: {'regular': ['0.132', '0.66', cache, None],
                    'long': ['0.264', '0.99', long_cache, '0.33']}
                    for model, cache, long_cache in (('gpt-6-sol', '0.0132', '0.0264'),
                                                    ('gpt-6.1-sol', '0.0066', '0.0132'))}}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def for_metadata(self, metadata, *, input_tokens):
        if (not isinstance(metadata, dict) or type(input_tokens) is not int or input_tokens < 0
                or metadata.get('model') not in MODELS or metadata.get('billing_tier') != 'paid'):
            raise ValueError('unpriced Panstar model/tier/input')
        try:
            day = date.fromisoformat(metadata['price_date'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('invalid Panstar price date') from None
        if day < date(2026, 11, 11):
            raise ValueError('Panstar catalog does not establish earlier prices')
        rates = list(self.as_dict()['models'][metadata['model']]['long' if input_tokens > 272000 else 'regular'])
        # Financial maximum is outside the 1M operational range; cover all priced cache writes before admission.
        if input_tokens == INPUT_BOUND:
            rates[0] = '0.33'
        values = [Decimal(value or '0') * 8 for value in rates]
        names = ('input_per_million', 'output_per_million', 'cache_read_per_million', 'cache_write_per_million')
        details = {'currency': 'CNY', 'model': metadata['model'], 'price_date': day.isoformat(),
                   'catalog_sha256': self.sha256, **{name: str(value) for name, value in zip(names, values)}}
        return PanstarPrice(*values, None, json.dumps(details, sort_keys=True), rates[3] is not None)

    def cost(self, *_args):
        raise ValueError('automatic selection has no uniform model/context price')

    def row_cost(self, row):
        calls = row.get('cost_calls')
        if not isinstance(calls, list) or not calls:
            raise ValueError('Panstar row lacks physical call costs')
        fields = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')
        total = Decimal(0)
        for call in calls:
            usage = call.get('usage')
            if call.get('status') != 'settled' or not isinstance(usage, dict):
                raise ValueError('Panstar row has unresolved usage')
            raw = native_usage({'usage': usage.get('raw_usage'), 'service_tier': usage.get('service_tier')})
            if (raw is None or any(usage.get(key) != value for key, value in raw.items())
                    or usage.get('served_model') != call.get('model')):
                raise ValueError('Panstar native usage/model differs from its physical receipt')
            amount = self.for_metadata(call, input_tokens=usage['input_tokens']).cost(*(usage[key] for key in fields))
            if Decimal(call['actual_rmb']) != amount:
                raise ValueError('Panstar physical cost differs from the dated quote')
            total += amount
        if any(sum(call['usage'][key] for call in calls) != row[key] for key in fields):
            raise ValueError('Panstar row totals differ from physical calls')
        return total


def native_usage(response):
    if not isinstance(response, dict):
        return None
    tier = response.get('service_tier')
    if tier is not None and (not isinstance(tier, str) or tier not in {'default', 'standard'}):
        return None
    raw = response.get('usage')
    if not isinstance(raw, dict):
        return None
    if any(raw.get(name) is not None and not isinstance(raw[name], dict)
           for name in ('input_tokens_details', 'output_tokens_details')):
        return None
    usage = ResponsesRelay._usage(response)
    details = raw.get('output_tokens_details') or {}
    reasoning = details.get('reasoning_tokens', 0) if isinstance(details, dict) else None
    if (usage is None or type(reasoning) is not int or not 0 <= reasoning <= usage['output_tokens']
            or type(raw.get('total_tokens')) is not int
            or raw['total_tokens'] != usage['input_tokens'] + usage['output_tokens']):
        return None
    return dict(usage, reasoning_tokens=reasoning)


class PanstarPolicy(GeminiPolicy):
    """Reuse signed-account/expiry/credential guards; keep Panstar's transport and prices distinct."""
    protocol = 'responses'
    provider = 'openai'
    endpoint = OFFICIAL_ENDPOINT

    def __init__(self, *, config=None, pricing=None, price_date=None):
        from openkyrozen.providers.config import ProviderConfig
        self.config = config or ProviderConfig(provider='openai', model_main='auto',
            model_simple=MODELS[0], model_complex=MODELS[1])
        self.pricing = pricing or PanstarPricing()
        self.price_date = price_date or datetime.now(timezone.utc).date().isoformat()
        self.role_models = {'simple': self.config.model_simple, 'complex': self.config.model_complex}
        if (self.config.provider != self.provider or self.config.model_main != 'auto'
                or len(set(self.role_models.values())) != 2 or set(self.role_models.values()) != set(MODELS)
                or type(self.pricing) is not PanstarPricing):
            raise ValueError('Panstar automatic selection requires the two predeclared inexpensive GPT roles')
        self.billing_record = self.billing_proof_sha256 = None

    def identity(self):
        return {'provider': self.provider, 'service': 'PanstarCloud', 'protocol': 'openai-responses',
                'model_main': 'auto', 'role_models': dict(self.role_models), 'price_date': self.price_date,
                'pricing_sha256': self.pricing.sha256, 'model_metadata_sha256': MODEL_METADATA_SHA256,
                'reservation_input_tokens': INPUT_BOUND, 'operational_input_token_limit': OPERATIONAL_INPUT_BOUND,
                'max_output_tokens_including_thoughts': OUTPUT_BOUND}

    def served_identity(self, model, served_model):
        return model in self.role_models.values() and served_model == model

    def response_usage(self, model, response):
        usage = native_usage(response)
        if (usage is None or response.get('status') not in ('completed', 'incomplete', 'failed')
                or not self.served_identity(model, response.get('model'))):
            return None
        return dict(usage, served_model=response['model'], raw_usage=copy.deepcopy(response['usage']),
                    service_tier=response.get('service_tier'))

    def accepts_call(self, call):
        if not super().accepts_call(call) or call.get('protocol') != 'openai-responses':
            return False
        usage = call.get('usage')
        if usage is None:
            return call.get('status') not in {'settled', 'overrun'}
        raw = native_usage({'usage': usage.get('raw_usage'), 'service_tier': usage.get('service_tier')})
        return (raw is not None and all(usage.get(key) == value for key, value in raw.items())
                and self.served_identity(call.get('model'), usage.get('served_model')))

    def validate_body(self, body):
        if (not isinstance(body, dict) or body.get('model') not in self.role_models.values()
                or set(body) - {'model', 'input', 'instructions', 'tools', 'tool_choice', 'parallel_tool_calls',
                                'temperature', 'top_p', 'max_output_tokens', 'stream', 'reasoning', 'store', 'include'}):
            raise ValueError('request exceeds the priced Panstar text/function policy')
        value = copy.deepcopy(body)
        if any(not isinstance(tool, dict) or tool.get('type') != 'function' for tool in value.get('tools', [])):
            raise ValueError('hosted tools have no admitted Panstar price')
        items = value.get('input')
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get('type') not in {None, 'message', 'function_call', 'function_call_output', 'reasoning'}:
                    raise ValueError('unpriced native input')
                content = item.get('content', item.get('output'))
                if isinstance(content, list) and any(not isinstance(part, dict) or part.get('type') not in
                        {'input_text', 'output_text', 'refusal'} for part in content):
                    raise ValueError('only native text/function content is admitted')
        elif not isinstance(items, str):
            raise ValueError('native input must be text or correlated native items')
        value['max_output_tokens'] = OUTPUT_BOUND
        return value


class PanstarRelay(ResponsesRelay):
    protocol = 'responses'

    def __init__(self, *, endpoint, api_key, budget, policy):
        parsed = urlsplit(endpoint)
        self.official = endpoint == OFFICIAL_ENDPOINT
        if not self.official and (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.path not in {'', '/v1'} or parsed.query or parsed.fragment or parsed.username):
            raise ValueError('Panstar transport must be its pinned endpoint or a synthetic loopback')
        if self.official:
            policy.check_transport()
            policy.check_credential(api_key)
        self.policy = policy
        self.root_actor_id_by_label = {}
        self._selected_models = {}
        super().__init__(endpoint=endpoint, api_key=api_key, budget=budget,
                         config=RelayConfig('auto', OUTPUT_BOUND, INPUT_BOUND, True), responses_policy=policy)

    def issue_actor_config(self, actor_id):
        config = super().issue_actor_config(actor_id)
        config.update(model_main='auto', model_simple=self.policy.role_models['simple'],
                      model_complex=self.policy.role_models['complex'], context_window_tokens=OPERATIONAL_INPUT_BOUND)
        return config

    def revoke_actor(self, actor_id):
        super().revoke_actor(actor_id)
        with self._lock:
            self._selected_models = {key: value for key, value in self._selected_models.items() if key[0] != actor_id}

    def selected_actor_model(self, actor_id):
        with self._lock:
            active = any(value['actor_id'] == actor_id and value['label'] == tuple(self.label)
                         for value in self._actor_credentials.values())
            model = self._selected_models.get((actor_id, tuple(self.label)))
            if not active or self.provider_error or model not in self.policy.role_models.values():
                raise ValueError('parent actor has no admitted selected model')
            return model


def run_smoke(relay, candidate):
    """The same real Responses SDK function/continuation check for both declared roles."""
    from openai import OpenAI
    if getattr(relay, 'smoke_verified_candidate', None) == candidate:
        return True
    relay.label = ('smoke', candidate, -1)
    before = len(relay.rows)
    client = OpenAI(api_key=relay.client_key, base_url=relay.url, max_retries=0, timeout=180)
    tools = [{'type': 'function', 'name': 'benchmark_echo', 'parameters': {
        'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}}]
    prompt = [{'role': 'user', 'content':
               'Call benchmark_echo with value ready, then repeat the returned value exactly.'}]
    try:
        for model in relay.policy.role_models.values():
            first = client.responses.create(model=model, input=prompt, tools=tools,
                tool_choice={'type': 'function', 'name': 'benchmark_echo'}, max_output_tokens=OUTPUT_BOUND)
            calls = [item for item in first.output if item.type == 'function_call']
            if (len(calls) != 1 or calls[0].name != 'benchmark_echo'
                    or json.loads(calls[0].arguments) != {'value': 'ready'}):
                relay.provider_error = relay.provider_error or 'native_smoke_function_contract'
                raise ValueError('native_smoke_function_contract')
            second = client.responses.create(model=model, input=prompt + [item.model_dump(exclude_none=True)
                for item in first.output] + [{'type': 'function_call_output', 'call_id': calls[0].call_id,
                    'output': '{"value":"ready"}'}], tools=tools, tool_choice='none', max_output_tokens=OUTPUT_BOUND)
            if not second.output_text or 'ready' not in second.output_text.lower() or any(
                    item.type == 'function_call' for item in second.output):
                relay.provider_error = relay.provider_error or 'native_smoke_continuation_contract'
                raise ValueError('native_smoke_continuation_contract')
        calls = relay.rows[before:]
        if len(calls) != 4 or any(call['status'] != 'settled' for call in calls):
            relay.provider_error = relay.provider_error or 'native_smoke_usage_contract'
            raise ValueError('native_smoke_usage_contract')
        relay.smoke_verified_candidate = candidate
        relay.smoke_verified_attempt_ids = tuple(call['attempt_id'] for call in calls)
        return True
    except Exception as exc:
        relay.provider_error = relay.provider_error or 'native_smoke_' + type(exc).__name__
        return False
    finally:
        client.close()


def main():
    import argparse
    from pathlib import Path
    from benchmarks.v3_deepseek import read_prior_exposure
    from benchmarks.v3_matched_live import run, _source_snapshot
    from scripts.publish_v3_preview import _verify_candidate_signature
    from openkyrozen.providers.config import ProviderConfig

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'repo', 'output', 'budget-db'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--candidate-sha', required=True)
    parser.add_argument('--prior-ledger', type=Path, action='append')
    parser.add_argument('--core-output', type=Path)
    parser.add_argument('--core-budget', type=Path)
    parser.add_argument('--model-routing', action='store_true')
    parser.add_argument('--billing-proof', type=Path)
    parser.add_argument('--billing-tier', choices=('paid',), required=True)
    parser.add_argument('--model-simple', default=MODELS[0])
    parser.add_argument('--model-complex', default=MODELS[1])
    parser.add_argument('--limit-cny', choices=('unlimited',), required=True)
    parser.add_argument('--planning-preflight-only', action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required; imports and offline tests never contact Panstar')
    candidate = _source_snapshot(args.source.resolve())
    if candidate != args.candidate_sha:
        raise ValueError('source differs from the frozen Panstar candidate')
    _verify_candidate_signature(args.source.resolve(), candidate)
    policy = PanstarPolicy(config=ProviderConfig(provider='openai', model_main='auto',
        model_simple=args.model_simple, model_complex=args.model_complex))
    if args.core_output is not None or args.core_budget is not None:
        if args.core_output is None or args.core_budget is None or args.planning_preflight_only or args.prior_ledger:
            raise ValueError('continuations require both original parent paths and no new prior admission')
        from benchmarks.v3_routing_supplement import run as continuation
        report = continuation(source=args.source, repo=args.repo, core_output=args.core_output,
            core_budget=args.core_budget, output=args.output, budget_path=args.budget_db,
            provider_policy=policy, billing_proof=args.billing_proof, model_routing=args.model_routing)
        passed = report.get('status') == 'complete' and report.get('summary', {}).get('benefit_passed') is True
    else:
        if args.model_routing or not args.prior_ledger:
            raise ValueError('core requires all original priors; model36 requires its completed54 origin')
        prior = read_prior_exposure(args.prior_ledger)
        report = run(source=args.source, repo=args.repo, output=args.output, budget_path=args.budget_db,
            limit_cny='unlimited', prior_hold_cny=str(prior.total_cny), prior_hold_records=prior.records,
            pricing=policy.pricing, relay_config=RelayConfig('auto', OUTPUT_BOUND, INPUT_BOUND, True),
            provider_policy=policy, billing_proof=args.billing_proof, planning_preflight_only=args.planning_preflight_only)
        passed = (report.get('status') == 'planning_preflight_passed' if args.planning_preflight_only else
                  report.get('status') == 'complete' and report.get('summary', {}).get('release_comparison_passed') is True)
    print(json.dumps(report.get('summary', {}), sort_keys=True, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
