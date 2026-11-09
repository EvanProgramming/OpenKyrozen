"""Offline native Gemini admission, accounting and SDK gateway contracts."""
from contextlib import closing
import copy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import requests

try:
    HAS_GOOGLE_SDK = importlib.util.find_spec('google.genai') is not None
except ModuleNotFoundError:
    HAS_GOOGLE_SDK = False

from benchmarks.v3_matched import BudgetLedger, Pricing


def fixture_price(input_rate, output_rate, cache_rate):
    values = {"currency": "CNY", "input_per_million": str(input_rate),
              "output_per_million": str(output_rate), "cache_read_per_million": str(cache_rate),
              "cache_write_per_million": str(input_rate), "provider": "synthetic offline test"}
    return Pricing(Decimal(input_rate), Decimal(output_rate), Decimal(cache_rate), Decimal(input_rate),
                   Decimal(15), json.dumps(values, sort_keys=True))


class FixtureCatalog:
    small = fixture_price("1", "2", "0.1")
    large = fixture_price("4", "8", "0.4")
    max_limit_cny = Decimal(15)

    def as_dict(self):
        return {"currency": "CNY", "models": {"small": self.small.as_dict(), "large": self.large.as_dict()}}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    def cost(self, *args):
        return self.small.cost(*args)

    def for_metadata(self, metadata, *, input_tokens):
        if not isinstance(metadata, dict) or metadata.get("model") not in {"small", "large"}:
            raise ValueError("unknown model price")
        return self.small if metadata["model"] == "small" else self.large


class ModelPricingLedgerTests(unittest.TestCase):
    def test_admission_serializes_metadata_before_pricing_callback_can_mutate_caller(self):
        metadata = {"model": "small"}
        class MutatingCatalog(FixtureCatalog):
            def for_metadata(self, snapshot, *, input_tokens):
                price = super().for_metadata(snapshot, input_tokens=input_tokens)
                metadata["model"] = "large"
                snapshot["model"] = "large"
                return price
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, limit_rmb=15, pricing=MutatingCatalog())
            self.assertTrue(budget.reserve("snapshot", input_token_bound=1000, output_token_bound=100,
                                           metadata=metadata))
            with closing(sqlite3.connect(path)) as db:
                reserved, persisted = db.execute("SELECT reserved_picormb,metadata_json FROM attempts").fetchone()
            self.assertEqual(metadata, {"model": "large"})
            self.assertEqual(reserved, 1_200_000_000)
            self.assertEqual(json.loads(persisted), {"model": "small"})

    def test_reserve_and_settle_use_stored_model_metadata_in_the_same_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, limit_rmb=15, pricing=FixtureCatalog())
            self.assertTrue(budget.reserve("large-call", input_token_bound=1000, output_token_bound=100,
                                           metadata={"model": "large"}))
            budget.settle("large-call", {"input_tokens": 100, "output_tokens": 25,
                                         "cache_read_tokens": 10, "cache_write_tokens": 0,
                                         "model": "small"})
            self.assertEqual(budget.spent_rmb, Decimal("0.000564"))
            with closing(sqlite3.connect(path)) as db:
                reserved, actual, metadata = db.execute(
                    "SELECT reserved_picormb,actual_picormb,metadata_json FROM attempts").fetchone()
            self.assertEqual(reserved, 4_800_000_000)
            self.assertEqual(actual, 564_000_000)
            self.assertEqual(json.loads(metadata), {"model": "large"})

    def test_unknown_model_has_no_admitted_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb=15, pricing=FixtureCatalog())
            with self.assertRaisesRegex(ValueError, "unknown model"):
                budget.reserve("unknown", input_token_bound=1000, output_token_bound=100,
                               metadata={"model": "not-priced"})
            self.assertEqual(budget.campaign_calls(), [])


class GeminiCatalogTests(unittest.TestCase):
    def test_default_pro_policy_blocks_before_billing_proof_or_credentials(self):
        from benchmarks.v3_gemini import GeminiPolicy, GeminiPricing
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb=15,
                prior_hold_rmb="12.151991600007", pricing=GeminiPricing())
            with self.assertRaisesRegex(ValueError, "both.*slot|reservation"):
                GeminiPolicy().check_budget(budget)
            self.assertEqual(budget.campaign_calls(), [])
            self.assertEqual(budget.remaining_rmb, Decimal("2.848008399993"))

    def test_catalog_prices_model_bands_and_calendar_without_uniform_repricing(self):
        from benchmarks.v3_gemini import GeminiPricing
        pricing = GeminiPricing()
        row = {"model": "gemini-3.1-pro-preview", "billing_tier": "paid", "price_date": "2026-11-09"}
        self.assertEqual(pricing.for_metadata(row, input_tokens=1_000_000).cost(1_000_000, 4096),
                         Decimal("32.589824"))
        self.assertEqual(pricing.reference_cost(row, 1_000_000, 4096), Decimal("27.3705636864"))
        self.assertEqual(pricing.for_metadata(row, input_tokens=200_000).cost(200_000, 10), Decimal("3.20096"))
        flash = {"model": "gemini-3.8-flash", "billing_tier": "paid", "price_date": "2026-12-31"}
        self.assertEqual(pricing.for_metadata(flash, input_tokens=100).cost(100, 20), Decimal("0.0012"))
        flash["price_date"] = "2027-01-01"
        self.assertEqual(pricing.for_metadata(flash, input_tokens=100).cost(100, 20), Decimal("0.0024"))
        for changed in ({"model": "other"}, {"billing_tier": "unknown"}, {"price_date": "not-a-date"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pricing.for_metadata(dict(row, **changed), input_tokens=100)

    def test_free_account_is_not_inferred_and_unavailable_cache_or_pro_is_not_priced(self):
        from benchmarks.v3_gemini import GeminiPolicy, GeminiPricing
        from openkyrozen.providers.config import ProviderConfig
        config = ProviderConfig(provider="google", model_simple="gemini-3.5-flash-lite",
                                model_complex="gemini-3.8-flash", model_main="auto")
        policy = GeminiPolicy(config=config, pricing=GeminiPricing(billing_tier="free"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "billing.*proof"):
                policy.verify_billing(None, "a" * 40)
        with self.assertRaises(ValueError):
            policy.pricing.for_metadata({"model": "gemini-3.1-pro-preview", "billing_tier": "free",
                                         "price_date": "2026-11-09"}, input_tokens=100)
        price = policy.pricing.for_metadata({"model": "gemini-3.5-flash-lite", "billing_tier": "free",
                                            "price_date": "2026-11-09"}, input_tokens=100)
        with self.assertRaises(ValueError):
            price.cost(100, 5, 10)

    def test_owner_signed_billing_binds_candidate_slots_key_expiry_and_supporting_bytes(self):
        from benchmarks.v3_gemini import GeminiPolicy, GeminiPricing, OFFICIAL_ENDPOINT
        from openkyrozen.providers.config import ProviderConfig
        from datetime import datetime, timezone
        policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-3.8-flash'),pricing=GeminiPricing(billing_tier='free'))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'proof.json';support=Path(directory)/'account.txt';support.write_bytes(b'synthetic account evidence')
            record={'schema_version':1,'provider':'google','official_endpoint':OFFICIAL_ENDPOINT,
                'candidate_sha':'a'*40,'role_models':policy.role_models,'model_main':'auto','billing_tier':'free',
                'credential_sha256':hashlib.sha256(b'synthetic-account-key').hexdigest(),
                'issued_at':'2026-11-09T00:00:00Z','expires_at':'2026-11-10T00:00:00Z',
                'account_unbilled_models':sorted(policy.role_models.values()),
                'account_billing_status':'free_verified','rate_policy':'account_free',
                'supporting_evidence':[{'path':str(support),'bytes':support.stat().st_size,
                    'sha256':hashlib.sha256(support.read_bytes()).hexdigest()}]}
            path.write_text(json.dumps(record,sort_keys=True,separators=(',',':')))
            Path(str(path)+'.asc').write_text('unit signature fixture')
            now=datetime(2026,11,9,12,tzinfo=timezone.utc)
            with self.assertRaises(ValueError):policy.verify_billing(path,'a'*40,verify_signature=lambda *_:False,now=now)
            result=policy.verify_billing(path,'a'*40,verify_signature=lambda *_:True,now=now)
            policy.check_credential('synthetic-account-key')
            self.assertNotIn('credential_sha256',result)
            with self.assertRaises(ValueError):policy.check_credential('wrong-account')
            with self.assertRaises(ValueError):policy.verify_billing(path,'b'*40,verify_signature=lambda *_:True,now=now)
            with self.assertRaises(ValueError):policy.verify_billing(path,'a'*40,verify_signature=lambda *_:True,
                now=datetime(2026,11,10,tzinfo=timezone.utc))
            support.write_text('changed')
            with self.assertRaises(ValueError):policy.verify_billing(path,'a'*40,verify_signature=lambda *_:True,now=now)


class NativeGatewayContractTests(unittest.TestCase):
    def test_documented_standard_service_tiers_only_preserve_raw_usage(self):
        from benchmarks.v3_gemini import native_usage
        raw={'promptTokenCount':10,'candidatesTokenCount':3,'totalTokenCount':13}
        for tier in ('standard','unspecified'):
            with self.subTest(tier=tier):
                value=dict(raw,serviceTier=tier);before=copy.deepcopy(value)
                self.assertEqual(native_usage({'usageMetadata':value})['output_tokens'],3)
                self.assertEqual(value,before)
        for tier in ('flex','priority','STANDARD','unknown',None,True,[]):
            with self.subTest(tier=tier):
                self.assertIsNone(native_usage({'usageMetadata':dict(raw,serviceTier=tier)}))

    def test_signature_only_metadata_requires_nonempty_string_and_optional_boolean_thought(self):
        from benchmarks.v3_gemini import validate_native_body
        for part in ({'thoughtSignature':'opaque'},{'thoughtSignature':'opaque','thought':True}):
            body={'contents':[{'role':'model','parts':[part]}]}
            self.assertEqual(validate_native_body(body)['contents'],body['contents'])
        for part in ({},{'thought':True},{'thoughtSignature':''},{'thoughtSignature':9},
                     {'thoughtSignature':'opaque','thought':'true'},{'thoughtSignature':'opaque','inlineData':{}}):
            with self.subTest(part=part),self.assertRaises(ValueError):
                validate_native_body({'contents':[{'parts':[part]}]})

    def test_served_aliases_are_exact_and_generic_38_version_is_not_a_model(self):
        from benchmarks.v3_gemini import served_identity
        self.assertTrue(served_identity('gemini-3.1-pro-preview',{'modelVersion':'gemini-3.1-pro-preview-01-2026'}))
        self.assertTrue(served_identity('gemini-3.8-flash',{'modelVersion':'gemini-3.8-flash'}))
        self.assertFalse(served_identity('gemini-3.8-flash',{'modelVersion':'gemini-3.0'}))
        self.assertFalse(served_identity('gemini-3.8-flash',{'modelVersion':'gemini-unpriced'}))
        self.assertTrue(served_identity('gemini-2.5-flash',{'modelVersion':'gemini-2.5-flash'}))
        self.assertFalse(served_identity('gemini-2.5-flash',{'modelVersion':'gemini-001'}))

    def test_native_usage_counts_thoughts_cache_and_rejects_unknown_counters(self):
        from benchmarks.v3_gemini import native_usage
        result = {"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 7,
                  "thoughtsTokenCount": 13, "cachedContentTokenCount": 20, "totalTokenCount": 120}}
        self.assertEqual(native_usage(result), {"input_tokens": 100, "output_tokens": 20,
                         "cache_read_tokens": 20, "cache_write_tokens": 0, "reasoning_tokens": 13})
        for changes in ({"totalTokenCount": 119}, {"toolUsePromptTokenCount": 1},
                        {"promptTokenCount": True}, {"newBillableTokenCount": 2}):
            with self.subTest(changes=changes):
                self.assertIsNone(native_usage({"usageMetadata": dict(result["usageMetadata"], **changes)}))

    def test_native_body_preserves_function_signature_and_forces_combined_output_bound(self):
        from benchmarks.v3_gemini import validate_native_body
        body = {"contents": [{"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {}},
                   "thoughtSignature": "opaque-bytes"}]}], "generationConfig": {"maxOutputTokens": 9000},
                "tools": [{"functionDeclarations": [{"name": "read_file", "parameters": {"type": "OBJECT"}}]}]}
        validated = validate_native_body(body)
        self.assertEqual(validated["generationConfig"]["maxOutputTokens"], 4096)
        self.assertEqual(validated["contents"], body["contents"])
        for key, value in (("cachedContent", "cache/x"), ("tools", [{"googleSearch": {}}]),
                           ("contents", [{"parts": [{"inlineData": {"data": "secret"}}]}]),
                           ("generationConfig", {"candidateCount": 2})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_native_body(dict(body, **{key: value}))


@unittest.skipUnless(HAS_GOOGLE_SDK, 'optional google-genai SDK is not installed')
class NativeSdkGatewayTests(unittest.TestCase):
    def test_full_financial_quote_retains_operational_million_failstop_and_actual_usage(self):
        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing,GeminiRelay,INPUT_BOUND
        from openkyrozen.providers import ProviderConfig
        import httpx
        policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-2.5-flash'),pricing=GeminiPricing())
        budget=BudgetLedger(Path(self.temp.name)/'paid-upper.db',limit_rmb=15,pricing=policy.pricing)
        relay=GeminiRelay(endpoint='http://127.0.0.1:1',api_key='synthetic-parent-key',budget=budget,policy=policy)
        relay.start();self.addCleanup(relay.close)
        config=relay.issue_actor_config('paid-root');model=policy.role_models['complex']
        raw={'promptTokenCount':1_000_001,'candidatesTokenCount':20,'thoughtsTokenCount':0,'totalTokenCount':1_000_021}
        with patch('benchmarks.v3_gemini.requests.post',return_value=self.response(model,usageMetadata=raw)) as upstream, \
             httpx.Client() as client:
            url=relay.url+'/v1beta/models/'+model+':generateContent'
            request={'headers':{'x-goog-api-key':config['api_key']},'json':{'contents':[{'parts':[{'text':'ready'}]}]}}
            self.assertEqual(client.post(url,**request).status_code,200)
            self.assertEqual(relay.provider_error,'native_usage_overrun')
            self.assertEqual(client.post(url,**request).status_code,503)
            self.assertEqual(upstream.call_count,1)
        self.assertEqual(relay.config.fixed_input_token_bound,INPUT_BOUND)
        self.assertEqual(config['context_window_tokens'],1_000_000)
        self.assertEqual(budget.campaign_calls()[0]['usage']['input_tokens'],1_000_001)
        with closing(sqlite3.connect(budget.path)) as db:
            self.assertEqual(db.execute('SELECT reserved_picormb FROM attempts').fetchone()[0],2_598_502_400_000)

    def setUp(self):
        from benchmarks.v3_gemini import GeminiRelay, GeminiPolicy, GeminiPricing
        from openkyrozen.providers.config import ProviderConfig
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = GeminiPolicy(config=ProviderConfig(provider="google", model_main="auto",
            model_simple="gemini-3.5-flash-lite", model_complex="gemini-3.8-flash"),
            pricing=GeminiPricing(billing_tier="free"), price_date="2026-11-09")
        self.budget = BudgetLedger(Path(self.temp.name) / "budget.sqlite3", limit_rmb=15, pricing=self.policy.pricing)
        self.relay = GeminiRelay(endpoint="http://127.0.0.1:1", api_key="synthetic-parent-secret",
                                  budget=self.budget, policy=self.policy)
        self.relay.label = ("v3", "fixture", 0)
        self.config = self.relay.issue_actor_config("root-actor")
        self.relay.start()
        self.addCleanup(self.relay.close)

    def response(self, model, parts=None, **changes):
        result = {"modelVersion": model, "candidates": [{"index": 0, "finishReason": "STOP",
            "content": {"role": "model", "parts": parts or [{"text": "ready"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 7,
                              "thoughtsTokenCount": 13, "totalTokenCount": 120}}
        result.update(changes)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(result).encode()
        response._content_consumed = True
        return response

    def test_real_sdk_native_function_continuation_and_parent_selected_model(self):
        from google import genai
        captured = []
        model = self.policy.role_models["simple"]
        def upstream(url, **options):
            captured.append((url, options))
            self.assertEqual(options["headers"]["x-goog-api-key"], "synthetic-parent-secret")
            self.assertNotIn(self.config["api_key"], repr(options))
            return self.response(model, [{"functionCall": {"name": "read_file", "args": {"path": "notes.txt"}},
                                          "thoughtSignature": "c2lnbmF0dXJl"}] if len(captured) == 1 else None)
        client = genai.Client(api_key=self.config["api_key"], http_options={"base_url": self.relay.url,
            "api_version": "v1beta", "retry_options": {"attempts": 1}})
        self.addCleanup(client.close)
        with patch("benchmarks.v3_gemini.requests.post", side_effect=upstream):
            response = client.models.generate_content(model=model, contents="inspect",
                config={"tools": [{"function_declarations": [{"name": "read_file", "parameters": {"type": "OBJECT"}}]}]})
            client.models.generate_content(model=model, contents=[{"role": "user", "parts": [{"text": "inspect"}]},
                response.candidates[0].content, {"role": "user", "parts": [{"function_response": {
                    "name": "read_file", "response": {"content": "ready"}}}]}])
        self.assertEqual(len(captured), 2)
        body = json.loads(captured[1][1]["data"])
        self.assertEqual(body["contents"][1]["parts"][0]["thoughtSignature"], "c2lnbmF0dXJl")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 4096)
        self.assertEqual(self.relay.selected_actor_model("root-actor"), model)
        calls = self.budget.campaign_calls()
        self.assertEqual([row["usage"]["output_tokens"] for row in calls], [20, 20])
        self.assertEqual(self.policy.pricing.row_cost(dict(input_tokens=200, output_tokens=40,
            cache_read_tokens=0, cache_write_tokens=0, cost_calls=calls)), 0)

    def test_actual_current_google_provider_retains_loopback_and_native_tool_continuation(self):
        from benchmarks.v3_gemini import make_google_provider
        from openkyrozen.providers import ProviderConfig
        from openkyrozen.providers.models import ToolReply
        model=self.policy.role_models['simple'];captured=[];sent=[]
        source=Path(self.temp.name)/'notes.txt';source.write_text('ready\n')
        tools=[{'name':'read_file','description':'Read an assigned file','parameters':{'type':'object',
            'properties':{'path':{'type':'string'}},'required':['path']}}]
        provider=make_google_provider(ProviderConfig(**self.config))
        transport_type=type(provider._client._api_client._httpx_client)
        original_send=transport_type.send
        def guarded_send(client,request,*args,**kwargs):
            self.assertEqual(request.url.host,'127.0.0.1','non-loopback SDK transport denied before send')
            self.assertEqual(request.url.scheme,'http')
            sent.append(str(request.url))
            return original_send(client,request,*args,**kwargs)
        def upstream(url,**options):
            captured.append(json.loads(options['data']))
            return self.response(model,[{'functionCall':{'name':'read_file','args':{'path':'notes.txt'}},
                'thoughtSignature':'b3BhcXVlLXNpZw=='}] if len(captured)==1 else [{'text':'ready'}])
        with patch.object(transport_type,'send',new=guarded_send), \
             patch('benchmarks.v3_gemini.requests.post',side_effect=upstream):
            try:
                messages=[{'role':'user','content':'Inspect notes.txt'}]
                first=provider.chat_response(messages,model=model,tools=tools)
                self.assertEqual(first.tool_calls[0].name,'read_file')
                final=provider.chat_response(messages+[first,ToolReply(first.tool_calls[0].id,'read_file',
                    {'data':{'content':source.read_text()},'error':None})],model=model,tools=tools)
                self.assertEqual(final.text,'ready')
                self.assertEqual(final.usage['completion_tokens'],20)
            finally:provider._client.close()
        self.assertEqual(len(sent),2)
        self.assertEqual(len(captured),2)
        self.assertEqual(captured[1]['contents'][1]['parts'][0]['thoughtSignature'],'b3BhcXVlLXNpZw==')
        self.assertEqual(captured[1]['contents'][-1]['parts'][0]['functionResponse']['response']['result']['data']['content'],'ready\n')
        self.assertEqual(len(self.budget.campaign_calls()),2)

    def test_actual_streamed_signature_only_part_replays_unchanged_to_next_request(self):
        from benchmarks.v3_gemini import make_google_provider
        from openkyrozen.providers import ProviderConfig
        from openkyrozen.providers.models import ResponseComplete
        captured=[];model=self.policy.role_models['simple']
        signature={'thoughtSignature':'b3BhcXVlLXNpZw=='}
        def upstream(url,**options):
            captured.append(json.loads(options['data']))
            if len(captured)==1:
                response=self.response(model,[{'text':'ready'},signature])
                response._content=b'data: '+response.content+b'\n\n'
                return response
            return self.response(model,[{'text':'continued'}])
        provider=make_google_provider(ProviderConfig(**self.config))
        transport_type=type(provider._client._api_client._httpx_client);original_send=transport_type.send
        def send(client,request,*args,**kwargs):
            self.assertEqual(request.url.host,'127.0.0.1')
            return original_send(client,request,*args,**kwargs)
        with patch.object(transport_type,'send',new=send),patch('benchmarks.v3_gemini.requests.post',side_effect=upstream):
            try:
                messages=[{'role':'user','content':'Say ready'}]
                events=list(provider.chat_response_stream(messages,model=model,tools=[]))
                first=next(event.response for event in events if isinstance(event,ResponseComplete))
                self.assertEqual(first.text,'ready')
                final=provider.chat_response(messages+[first,{'role':'user','content':'Continue'}],model=model,tools=[])
                self.assertEqual(final.text,'continued')
            finally:provider._client.close()
        self.assertEqual(len(captured),2)
        self.assertEqual(captured[1]['contents'][1]['parts'][1],signature)
        self.assertEqual(len(self.budget.campaign_calls()),2)

    def test_unknown_served_identity_retains_hold_and_revocation_blocks_new_requests(self):
        model = self.policy.role_models["simple"]
        import httpx
        with patch("benchmarks.v3_gemini.requests.post", return_value=self.response("gemini-unpriced")) as upstream:
            with httpx.Client() as client:
                response = client.post(self.relay.url + "/v1beta/models/" + model + ":generateContent",
                    headers={"x-goog-api-key": self.config["api_key"]}, json={"contents": [{"parts": [{"text": "x"}]}]})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(self.budget.campaign_calls()[0]["status"], "unknown")
        with self.assertRaises(ValueError):
            self.relay.selected_actor_model("root-actor")
        self.relay.revoke_actor("root-actor")
        # httpx avoids the patched requests.post used for the synthetic upstream.
        with httpx.Client() as client:
            denied = client.post(self.relay.url + "/v1beta/models/" + model + ":generateContent",
                headers={"x-goog-api-key": self.config["api_key"]}, json={"contents": [{"parts": [{"text": "x"}]}]})
        self.assertEqual(denied.status_code, 401)

    def test_stream_uses_last_cumulative_usage_once_and_cancelled_transport_retains_hold(self):
        from google import genai
        model=self.policy.role_models['complex']
        client=genai.Client(api_key=self.config['api_key'],http_options={'base_url':self.relay.url,
            'api_version':'v1beta','retry_options':{'attempts':1}})
        self.addCleanup(client.close)
        result=json.loads(self.response(model).content)
        first=dict(result,usageMetadata={'promptTokenCount':90,'candidatesTokenCount':2,'thoughtsTokenCount':3,'totalTokenCount':95})
        response=requests.Response();response.status_code=200;response._content_consumed=True
        response._content=b''.join(b'data: '+json.dumps(item).encode()+b'\n\n' for item in [first,result])
        with patch('benchmarks.v3_gemini.requests.post',return_value=response):
            chunks=list(client.models.generate_content_stream(model=model,contents='ready'))
        self.assertEqual(len(chunks),2)
        calls=self.budget.campaign_calls()
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['usage']['input_tokens'],100)
        self.assertEqual(calls[0]['usage']['output_tokens'],20)
        class BrokenResponse:
            status_code=200
            def iter_content(self,**_):
                yield b'data: '+json.dumps(first).encode()+b'\n\n'
                raise requests.ConnectionError('synthetic closed stream')
            def close(self):pass
        with patch('benchmarks.v3_gemini.requests.post',return_value=BrokenResponse()):
            try:list(client.models.generate_content_stream(model=model,contents='ready'))
            except Exception:pass
        self.assertEqual([call['status'] for call in self.budget.campaign_calls()].count('unknown'),1)
        self.assertIsNotNone(self.relay.provider_error)

    def test_native_smoke_charges_four_physical_calls_for_both_role_slots(self):
        from benchmarks.v3_gemini import run_smoke
        captured=[]
        def upstream(url,**options):
            body=json.loads(options['data']);model=url.split('/models/')[1].split(':')[0]
            captured.append((model,body))
            return self.response(model,[{'functionCall':{'name':'benchmark_echo','args':{'value':'ready'}},
                'thoughtSignature':'c2ln'}] if len(captured)%2 else [{'text':'ready'}])
        with patch('benchmarks.v3_gemini.requests.post',side_effect=upstream):
            self.assertTrue(run_smoke(self.relay,'a'*40),self.relay.provider_error)
            self.assertTrue(run_smoke(self.relay,'a'*40))
        self.assertEqual([model for model,_ in captured],['gemini-3.5-flash-lite']*2+['gemini-3.8-flash']*2)
        self.assertEqual(len(self.budget.campaign_calls()),4)
        self.assertEqual(len(self.relay.smoke_verified_attempt_ids),4)

    def test_stream_keeps_phase_lock_and_second_physical_call_waiting_until_cleanup(self):
        from google import genai
        started,release,second_reached=threading.Event(),threading.Event(),threading.Event()
        model=self.policy.role_models['complex'];errors=[];seen=[]
        encoded=b'data: '+self.response(model).content+b'\n\n'
        class Response:
            status_code=200
            def __init__(self,first):self.first=first
            def iter_content(self,**_):
                yield encoded
                if self.first:
                    started.set()
                    if not release.wait(3):raise requests.Timeout('synthetic barrier expired')
            def close(self):pass
        def upstream(*_,**__):
            seen.append(1)
            if len(seen)==2:second_reached.set()
            return Response(len(seen)==1)
        def consume():
            client=genai.Client(api_key=self.config['api_key'],http_options={'base_url':self.relay.url,
                'api_version':'v1beta','retry_options':{'attempts':1}})
            try:list(client.models.generate_content_stream(model=model,contents='ready'))
            except Exception as exc:errors.append(type(exc).__name__)
            finally:client.close()
        with patch('benchmarks.v3_gemini.requests.post',side_effect=upstream):
            first=threading.Thread(target=consume);first.start()
            self.assertTrue(started.wait(2))
            second=threading.Thread(target=consume);second.start()
            acquired=self.relay._upstream_lock.acquire(timeout=.05)
            if acquired:self.relay._upstream_lock.release()
            try:
                self.assertFalse(acquired,'phase closure acquired lock before stream terminal cleanup')
                self.assertFalse(second_reached.wait(.05))
            finally:
                release.set();first.join(3);second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(errors,errors)
        self.assertEqual(len(seen),2)
        self.assertEqual([call['status'] for call in self.budget.campaign_calls()],['settled','settled'])


class GeminiPriorTests(unittest.TestCase):
    def test_old_native_quote_is_rejected_instead_of_reused_as_full_backend_reservation(self):
        from benchmarks.v3_deepseek import read_prior_exposure
        policy=GeminiAlternativeProfileTests().policy()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old-quote.db';budget=BudgetLedger(path,limit_rmb=15,pricing=policy.pricing)
            metadata=dict(policy.metadata('gemini-2.5-flash'),reserved_rmb='2.48192',max_output_tokens=4096)
            budget.reserve('under-reserved-native',input_token_bound=1_000_000,output_token_bound=4096,metadata=metadata)
            budget.settle('under-reserved-native',None)
            with self.assertRaisesRegex(ValueError,'reservation'):
                read_prior_exposure([path])

    def test_future_native_prior_preserves_model_price_metadata_and_mixed_ancestry(self):
        from benchmarks.v3_gemini import GeminiPricing, GeminiPolicy
        from benchmarks.v3_deepseek import read_prior_exposure
        from openkyrozen.providers.config import ProviderConfig
        with tempfile.TemporaryDirectory() as directory:
            first, second = (Path(directory) / name for name in ("first.sqlite3", "second.sqlite3"))
            pricing = GeminiPricing(billing_tier="free")
            policy = GeminiPolicy(config=ProviderConfig(provider="google", model_main="auto",
                model_simple="gemini-3.5-flash-lite", model_complex="gemini-3.8-flash"), pricing=pricing)
            budget = BudgetLedger(first, limit_rmb=15, pricing=pricing)
            metadata = dict(policy.metadata("gemini-3.8-flash"), reserved_rmb="0", max_output_tokens=4096)
            budget.reserve("native-free", input_token_bound=1_048_576, output_token_bound=4096, metadata=metadata)
            budget.settle("native-free", {"input_tokens": 100, "output_tokens": 20,
                "cache_read_tokens": 5, "cache_write_tokens": 0, 'reasoning_tokens':3,
                'served_model':'gemini-3.8-flash','raw_usage':{'promptTokenCount':100,'candidatesTokenCount':17,
                    'thoughtsTokenCount':3,'cachedContentTokenCount':5,'totalTokenCount':120}})
            exposure = read_prior_exposure([first])
            self.assertEqual(exposure.total_cny, 0)
            self.assertEqual(json.loads(exposure.records[0])["metadata"], metadata)
            carried = BudgetLedger(second, limit_rmb=15, pricing=pricing, prior_hold_rmb="0",
                prior_hold_records=exposure.records)
            again = read_prior_exposure([first, second])
            self.assertEqual(again.records, exposure.records)
            self.assertEqual(again.attempt_count, 1)
            self.assertEqual(carried.remaining_rmb, 15)


class GeminiAlternativeProfileTests(unittest.TestCase):
    def test_financial_bound_covers_backend_max_without_relaxing_operational_million(self):
        from benchmarks import v3_gemini as native
        policy=self.policy()
        self.assertEqual(native.INPUT_BOUND,1_048_576)
        self.assertEqual(native.OPERATIONAL_INPUT_BOUND,1_000_000)
        self.assertEqual(policy.identity()['operational_input_token_limit'],1_000_000)
        self.assertEqual(policy.pricing.for_metadata(policy.metadata('gemini-2.5-flash'),input_tokens=native.INPUT_BOUND).cost(
            native.INPUT_BOUND,4096),Decimal('2.5985024'))

    def policy(self,**kwargs):
        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing
        from openkyrozen.providers import ProviderConfig
        return GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-2.5-flash'),
            pricing=GeminiPricing(billing_tier=kwargs.pop('billing_tier','paid')),**kwargs)

    def test_both_paid_slots_fit_one_reservation_but_not_full_campaign_and_cutoff_blocks(self):
        policy=self.policy(price_date='2026-11-09')
        with tempfile.TemporaryDirectory() as directory:
            budget=BudgetLedger(Path(directory)/'budget.db',limit_rmb=15,
                prior_hold_rmb='12.151991600007',pricing=policy.pricing)
            policy.check_budget(budget)
            self.assertEqual([policy.pricing.for_metadata(policy.metadata(model),input_tokens=1_048_576).cost(1_048_576,4096)
                for model in policy.role_models.values()],[Decimal('2.5985024')]*2)
            self.assertEqual(budget.campaign_calls(),[])
            self.assertLess(budget.remaining_rmb,Decimal('2.5985024')*2)
        for day in ('2026-12-01','2027-01-01'):
            with self.subTest(day=day),self.assertRaisesRegex(ValueError,'rerout|cutoff'):
                self.policy(price_date=day)

    def test_affordable_paid_profile_still_requires_proof_before_key_or_http(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_responses_relay import RelayConfig
        from tests.test_v3_matched_live import prepare_candidate_apparatus
        policy=self.policy()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);prepare_candidate_apparatus(root/'source')
            with patch.object(live,'_source_snapshot',return_value='a'*40), \
                 patch('benchmarks.v3_protected_worker.verify_worker_protection',return_value={}), \
                 patch('openkyrozen.tools.sandbox.sandbox_command',return_value=type('Probe',(),{'backend':'fixture','close':lambda _:None})()), \
                 patch.object(policy,'credential') as credential,patch('benchmarks.v3_gemini.requests.post') as http:
                with self.assertRaisesRegex(ValueError,'billing proof'):
                    live.run(source=root/'source',repo=root,output=root/'report.json',budget_path=root/'budget.db',
                        limit_cny='15',prior_hold_cny='12.151991600007',pricing=policy.pricing,
                        relay_config=RelayConfig('auto',4096,1_048_576,True),provider_policy=policy)
                credential.assert_not_called();http.assert_not_called()

    def test_signed_paid_rate_upper_preserves_unverified_account_fact(self):
        from datetime import datetime,timezone
        from benchmarks.v3_gemini import OFFICIAL_ENDPOINT
        policy=self.policy(price_date='2026-11-09')
        with tempfile.TemporaryDirectory() as directory:
            support=Path(directory)/'support.txt';support.write_text('synthetic official rate/cap authorization, account unverified')
            path=Path(directory)/'proof.json'
            record={'schema_version':1,'provider':'google','official_endpoint':OFFICIAL_ENDPOINT,
                'candidate_sha':'a'*40,'role_models':policy.role_models,'model_main':'auto','billing_tier':'paid',
                'account_billing_status':'unverified','rate_policy':'standard_paid_rate_upper','conservative_cny_per_usd':'8',
                'credential_sha256':hashlib.sha256(b'synthetic-key').hexdigest(),
                'issued_at':'2026-11-09T00:00:00Z','expires_at':'2026-11-10T00:00:00Z',
                'supporting_evidence':[{'path':str(support),'bytes':support.stat().st_size,
                    'sha256':hashlib.sha256(support.read_bytes()).hexdigest()}]}
            path.write_text(json.dumps(record,sort_keys=True,separators=(',',':')))
            Path(str(path)+'.asc').write_text('synthetic unit signature')
            now=datetime(2026,11,9,12,tzinfo=timezone.utc)
            result=policy.verify_billing(path,'a'*40,verify_signature=lambda *_:True,now=now)
            self.assertEqual(result['account_billing_status'],'unverified')
            self.assertEqual(result['rate_policy'],'standard_paid_rate_upper')
            policy.check_credential('synthetic-key')
            self.assertEqual(policy.metadata('gemini-2.5-flash')['account_billing_status'],'unverified')
            record.update(billing_tier='free',rate_policy='account_free',account_unbilled_models=sorted(policy.role_models.values()))
            path.write_text(json.dumps(record,sort_keys=True,separators=(',',':')))
            with self.assertRaises(ValueError):
                self.policy(price_date='2026-11-09',billing_tier='free').verify_billing(path,'a'*40,verify_signature=lambda *_:True,now=now)


class GeminiRunnerTests(unittest.TestCase):
    def test_native_known_operational_counters_are_required_but_unknown_holds_stay_unknown(self):
        from benchmarks.v3_gemini import GeminiPolicy
        policy=GeminiPolicy();base=dict(policy.metadata(policy.role_models['simple']),max_output_tokens=4096)
        self.assertTrue(policy.accepts_call(dict(base,status='settled',usage={'input_tokens':1_000_000,'output_tokens':4096})))
        for key,value in [('input_tokens',1_000_001),('output_tokens',4097),('input_tokens',True),
                          ('output_tokens',2.0),('input_tokens',-1),('output_tokens',None)]:
            with self.subTest(key=key,value=value):
                usage=dict(input_tokens=100,output_tokens=20);usage[key]=value
                self.assertFalse(policy.accepts_call(dict(base,status='settled',usage=usage)))
        self.assertFalse(policy.accepts_call(dict(base,status='settled',usage=None)))
        for status in ('reserved','unknown'):
            self.assertTrue(policy.accepts_call(dict(base,status=status,usage=None)))

    def test_native_core_and54_origin_reject_known_overrun_before_key_or_checkpoint_rewrite(self):
        from benchmarks import v3_matched_live as live,v3_routing_supplement as routing
        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing,INPUT_BOUND
        from benchmarks.v3_responses_relay import RelayConfig
        from openkyrozen.providers import ProviderConfig
        from tests.test_v3_matched_live import prepare_candidate_apparatus
        from dataclasses import asdict
        policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-3.8-flash'),pricing=GeminiPricing(billing_tier='free'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';prepare_candidate_apparatus(source)
            budget_path=root/'budget.db';output=root/'report.json'
            ledger=BudgetLedger(budget_path,limit_rmb=15,pricing=policy.pricing)
            metadata=dict(policy.metadata(policy.role_models['simple']),arm='v3',fixture_id='investigation-1',repeat=0,
                          max_output_tokens=4096,reserved_rmb='0')
            ledger.reserve('known-overrun',input_token_bound=INPUT_BOUND,output_token_bound=4096,metadata=metadata)
            ledger.settle('known-overrun',{'input_tokens':1_000_001,'output_tokens':20,'cache_read_tokens':0,
                'cache_write_tokens':0,'reasoning_tokens':3,'served_model':policy.role_models['simple'],
                'raw_usage':{'promptTokenCount':1_000_001,'candidatesTokenCount':17,'thoughtsTokenCount':3,'totalTokenCount':1_000_021}})
            identity=live.apparatus_identity();identity['provider_policy']=policy.identity()
            report={**identity,'protocol':'openkyrozen-v3-matched-live-v1','candidate_sha':'a'*40,'baseline_sha':live.BASELINE,
                'model':'auto','repeats':3,'pricing':policy.pricing.as_dict(),'relay_config':asdict(RelayConfig('auto',4096,INPUT_BOUND,True)),
                'budget':{'prior_campaign_hold_cny':'0'},'status':'blocked_provider_error','rows':[],'calls':[]}
            live._write_checkpoint(output,budget_path,report);before=output.read_bytes()
            with patch.object(live,'_source_snapshot',return_value='a'*40), \
                 patch('benchmarks.v3_protected_worker.verify_worker_protection'), \
                 patch('openkyrozen.tools.sandbox.sandbox_command',return_value=type('Probe',(),{'backend':'fixture','close':lambda _:None})()), \
                 patch.object(policy,'credential') as credential,patch.object(policy,'verify_billing') as proof, \
                 patch('benchmarks.v3_gemini.requests.post') as http:
                with self.assertRaisesRegex(ValueError,'journal.*cohort|native physical'):
                    live.run(source=source,repo=root,output=output,budget_path=budget_path,limit_cny='15',prior_hold_cny='0',
                        pricing=policy.pricing,relay_config=RelayConfig('auto',4096,INPUT_BOUND,True),provider_policy=policy)
                self.assertEqual(output.read_bytes(),before)
                # Even a formerly data-complete parent cannot pass its actual physical overrun to54.
                report['status']='complete';live._write_checkpoint(output,budget_path,report);before=output.read_bytes()
                with self.assertRaisesRegex(ValueError,'native physical'):
                    routing.run(source=source,repo=root,core_output=output,core_budget=budget_path,
                        output=root/'54.json',budget_path=root/'54.db',provider_policy=policy)
                self.assertEqual(output.read_bytes(),before)
                self.assertFalse((root/'54.db').exists())
                credential.assert_not_called();proof.assert_not_called();http.assert_not_called()

    def test_official_gateway_cannot_be_constructed_without_verified_account_proof(self):
        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing,GeminiRelay,OFFICIAL_ENDPOINT
        from openkyrozen.providers import ProviderConfig
        policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-3.8-flash'),pricing=GeminiPricing(billing_tier='free'))
        with tempfile.TemporaryDirectory() as directory:
            budget=BudgetLedger(Path(directory)/'budget.db',limit_rmb=15,pricing=policy.pricing)
            with patch('benchmarks.v3_gemini.requests.post') as http:
                with self.assertRaisesRegex(ValueError,'proof_expired_or_absent'):
                    GeminiRelay(endpoint=OFFICIAL_ENDPOINT,api_key='synthetic-key',budget=budget,policy=policy)
                http.assert_not_called()

    def test_native_policy_reuses_exact_180_loop_and_per_call_costs_without_claiming_success(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_gemini import GeminiPolicy,GeminiPricing,GeminiRelay
        from benchmarks.v3_responses_relay import RelayConfig
        from openkyrozen.providers import ProviderConfig
        from tests.test_v3_matched_live import prepare_candidate_apparatus,valid_worker_result
        policy=GeminiPolicy(config=ProviderConfig(provider='google',model_main='auto',
            model_simple='gemini-3.5-flash-lite',model_complex='gemini-3.8-flash'),pricing=GeminiPricing(billing_tier='free'))
        relay_holder=[];commands=[]
        class Relay(GeminiRelay):
            def __init__(self,**options):
                super().__init__(**options);relay_holder.append(self)
        def call(relay):
            model=list(policy.role_models.values())[len(relay.rows)%2]
            arm,fixture,repeat=relay.label
            metadata=dict(policy.metadata(model),attempt_id='call-'+str(len(relay.rows)),arm=arm,
                fixture_id=fixture,repeat=repeat,max_output_tokens=4096,reserved_rmb='0')
            relay.budget.reserve(metadata['attempt_id'],input_token_bound=1_048_576,output_token_bound=4096,metadata=metadata)
            relay.budget.settle(metadata['attempt_id'],{'input_tokens':100,'output_tokens':20,'reasoning_tokens':3,
                'cache_read_tokens':0,'cache_write_tokens':0,'served_model':model,
                'raw_usage':{'promptTokenCount':100,'candidatesTokenCount':17,'thoughtsTokenCount':3,'totalTokenCount':120}})
            relay.rows[:]=relay.budget.campaign_calls()
        def smoke(relay,candidate):
            relay.label=('smoke',candidate,-1)
            for _ in range(4):call(relay)
            relay.smoke_verified_attempt_ids=tuple(row['attempt_id'] for row in relay.rows)
            return True
        def worker(command,**options):
            commands.append(command);call(relay_holder[0])
            self.assertIsNotNone(options['selected_actor_model'])
            Path(command[command.index('--result')+1]).write_text(json.dumps(valid_worker_result()))
            return 0,''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);prepare_candidate_apparatus(root/'source')
            with patch.object(live,'_source_snapshot',return_value='a'*40), \
                 patch('benchmarks.v3_protected_worker.verify_worker_protection',return_value={}), \
                 patch('openkyrozen.tools.sandbox.sandbox_command',return_value=type('Probe',(),{'backend':'fixture','close':lambda _:None})()), \
                 patch.object(policy,'verify_billing',return_value={'proof_sha256':'b'*64}), \
                 patch.object(policy,'credential',return_value=('http://127.0.0.1:1','synthetic-key')), \
                 patch('benchmarks.v3_gemini.GeminiRelay',Relay),patch('benchmarks.v3_gemini.run_smoke',side_effect=smoke), \
                 patch('benchmarks.v3_deepseek.run_planning_preflight',return_value={'status':'passed'}), \
                 patch.object(live,'_extract_baseline'),patch.object(live,'_run_worker',side_effect=worker), \
                 patch('builtins.print'),patch.object(live,'verify_fixture',return_value={
                     'result':'failed','safety_passed':True,'safety_components':{'scope_safe':True,
                     'original_tests_unchanged':True,'provider_ok':True,'no_running_processes':True},
                     'plan_scenario_passed':False,'verified_success':False,'changed_paths':[]}):
                report=live.run(source=root/'source',repo=root,output=root/'report.json',budget_path=root/'budget.sqlite3',
                    limit_cny='15',prior_hold_cny='12.151991600007',pricing=policy.pricing,
                    relay_config=RelayConfig('auto',4096,1_048_576,True),provider_policy=policy)
            self.assertEqual(len(commands),180)
            self.assertTrue(all(command[command.index('--protocol')+1]=='google' for command in commands))
            self.assertTrue(all(command[command.index('--model')+1]=='auto' for command in commands))
            self.assertEqual(len(report['rows']),180)
            self.assertEqual(len(report['calls']),184)
            self.assertTrue(report['summary']['complete'])
            self.assertFalse(report['summary']['release_comparison_passed'])
            self.assertEqual(report['budget']['remaining_cny'],'2.848008399993')
            self.assertEqual({call['model'] for row in report['rows'] for call in row['cost_calls']},set(policy.role_models.values()))

    def test_unaffordable_default_blocks_before_proof_key_sdk_smoke_or_http(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_gemini import GeminiPolicy
        from benchmarks.v3_responses_relay import RelayConfig
        from tests.test_v3_matched_live import prepare_candidate_apparatus
        policy=GeminiPolicy()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);prepare_candidate_apparatus(root/'source')
            with patch.object(live,'_source_snapshot',return_value='a'*40), \
                 patch('benchmarks.v3_protected_worker.verify_worker_protection',return_value={}), \
                 patch('openkyrozen.tools.sandbox.sandbox_command',return_value=type('Probe',(),{'backend':'fixture','close':lambda _:None})()), \
                 patch.object(policy,'verify_billing') as proof,patch.object(policy,'credential') as credential, \
                 patch('benchmarks.v3_gemini.GeminiRelay') as relay,patch('benchmarks.v3_gemini.run_smoke') as smoke, \
                 patch('benchmarks.v3_gemini.requests.post') as http:
                with self.assertRaisesRegex(ValueError,'both-slot reservation'):
                    live.run(source=root/'source',repo=root,output=root/'output.json',budget_path=root/'budget.sqlite3',
                        limit_cny='15',prior_hold_cny='12.151991600007',pricing=policy.pricing,
                        relay_config=RelayConfig('auto',4096,1_048_576,True),provider_policy=policy)
                for operation in (proof,credential,relay,smoke,http):operation.assert_not_called()

    def test_native_routing_observations_keep_intervention_and_slot_guards(self):
        from benchmarks.v3_gemini import GeminiPolicy
        from benchmarks.v3_routing_supplement import validate_observations,intervene
        from openkyrozen.routing.router import RoutingDecision
        from dataclasses import asdict
        policy=GeminiPolicy()
        before=RoutingDecision('google',policy.role_models['complex'],'standard',None,'coder',True)
        row={'before':asdict(before),'after':asdict(intervene(before,'planning-never',profile_override='auto',interaction_mode='agent')),
             'variant':'planning-never','profile_override':'auto','interaction_mode':'agent',
             'system_one_backend':'off','decision_assist_backend':'off','requests':[{'model':policy.role_models['complex'],
                'reserve_tokens':4096,'message_projection_sha256':'a'*64}]}
        self.assertTrue(validate_observations([row],'planning-never',policy))
        row['after']['profile']='researcher'
        self.assertFalse(validate_observations([row],'planning-never',policy))
        row['after']['profile']='coder';row['requests'][0]['model']='unpriced'
        self.assertFalse(validate_observations([row],'planning-never',policy))

class GeminiCliCompletionTests(unittest.TestCase):
    def test_native_cli_requires_complete_status_and_existing_success_predicate_in_all_three_phases(self):
        from benchmarks import v3_gemini as native
        from types import SimpleNamespace
        cases=[('blocked_provider_error',True,1),('blocked_budget_reservation',True,1),
               ('incomplete',True,1),(None,True,1),('complete',False,1),('complete',True,0)]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for phase in ('180','54','36'):
                for status,passed,exit_code in cases:
                    with self.subTest(phase=phase,status=status,passed=passed):
                        predicate='release_comparison_passed' if phase=='180' else 'benefit_passed'
                        report={'summary':{predicate:passed}}
                        if status is not None:report['status']=status
                        argv=['gemini','--source',str(root/'source'),'--repo',str(root/'repo'),
                            '--candidate-sha','a'*40,'--output',str(root/'report.json'),'--budget-db',str(root/'budget.db'),
                            '--billing-tier','paid','--execute']
                        if phase=='180':argv+=['--prior-ledger',str(root/'prior.db')]
                        else:argv+=['--core-output',str(root/'parent.json'),'--core-budget',str(root/'parent.db')]
                        if phase=='36':argv+=['--model-routing']
                        with patch('sys.argv',argv),patch('builtins.print'), \
                             patch('benchmarks.v3_matched_live._source_snapshot',return_value='a'*40), \
                             patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                             patch('benchmarks.v3_deepseek.read_prior_exposure',return_value=SimpleNamespace(total_cny=Decimal(0),records=())), \
                             patch('benchmarks.v3_matched_live.run',return_value=report) as core, \
                             patch('benchmarks.v3_routing_supplement.run',return_value=report) as continuation, \
                             patch.object(native.GeminiPolicy,'credential') as credential, \
                             patch.object(native,'GeminiRelay') as gateway,patch.object(native.requests,'post') as http:
                            with self.assertRaises(SystemExit) as result:native.main()
                            self.assertEqual(result.exception.code,exit_code)
                            self.assertEqual(core.call_count,1 if phase=='180' else 0)
                            self.assertEqual(continuation.call_count,0 if phase=='180' else 1)
                            if phase!='180':self.assertEqual(continuation.call_args.kwargs['model_routing'],phase=='36')
                            credential.assert_not_called();gateway.assert_not_called();http.assert_not_called()

    def test_preflight_cli_still_requires_its_exact_original_status(self):
        from benchmarks import v3_gemini as native
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            argv=['gemini','--source',str(root/'source'),'--repo',str(root/'repo'),'--candidate-sha','a'*40,
                '--output',str(root/'report.json'),'--budget-db',str(root/'budget.db'),'--prior-ledger',str(root/'prior.db'),
                '--billing-tier','paid','--planning-preflight-only','--execute']
            for status,expected in [('planning_preflight_passed',0),('complete',1),('blocked_provider_error',1)]:
                with self.subTest(status=status),patch('sys.argv',argv),patch('builtins.print'), \
                     patch('benchmarks.v3_matched_live._source_snapshot',return_value='a'*40), \
                     patch('scripts.publish_v3_preview._verify_candidate_signature'), \
                     patch('benchmarks.v3_deepseek.read_prior_exposure',return_value=SimpleNamespace(total_cny=Decimal(0),records=())), \
                     patch('benchmarks.v3_matched_live.run',return_value={'status':status,'summary':{'release_comparison_passed':True}}), \
                     patch.object(native.GeminiPolicy,'credential') as credential,patch.object(native.requests,'post') as http:
                    with self.assertRaises(SystemExit) as result:native.main()
                    self.assertEqual(result.exception.code,expected)
                    credential.assert_not_called();http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
