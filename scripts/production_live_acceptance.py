#!/usr/bin/env python3
"""Bounded real-provider checks using synthetic content and read-only credentials."""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
if os.environ.get('KYROZEN_ACCEPTANCE_INSTALLED')!='1':
    sys.path.insert(0,str(ROOT))


def acceptance(limit, focused=False, budget_path=None, cancellation_only=False):
    saved=json.loads((Path.home()/'.kyrozen_config.json').read_text())
    encrypted=saved.get('api_key','')
    if encrypted.startswith('v2:'):
        from cryptography.fernet import Fernet
        key=Fernet((Path.home()/'.kyrozen_secret').read_bytes().strip()).decrypt(encrypted[3:].encode()).decode()
    else:
        from openkyrozen.security.credentials import decrypt_api_key
        key=decrypt_api_key(encrypted)
    if not key:
        raise RuntimeError('BLOCKED: configured provider credential unavailable')
    from openkyrozen.providers.config import ProviderConfig
    from openkyrozen.providers.factory import get_provider
    from openkyrozen.app.bootstrap import build_application,build_memory
    from openkyrozen.tools import ToolAdapters
    from openkyrozen.skills.registry import SkillRegistry
    lock=threading.Lock()
    request_started=threading.Event()
    request_release=threading.Event()
    cancellation_armed=threading.Event()
    requests=body_bytes=0
    providers=[]
    response_metadata=[]
    from audit_budget import AuditBudget
    price_day=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    offpeak=price_day.weekday()>=5
    budget=AuditBudget(budget_path or Path(tempfile.gettempdir())/"openkyrozen-live-5rmb.json",
        rates=(4.5,13.5) if offpeak else (9,27),wait_seconds=60)
    local=threading.local()
    def guard(request):
        nonlocal requests,body_bytes
        if offpeak and datetime.now(ZoneInfo('Asia/Shanghai')).date()!=price_day:
            raise RuntimeError('Pricing window changed; restart the bounded audit')
        size=len(request.content)
        with lock:
            if requests>=limit or size>100000 or body_bytes+size>1000000:
                raise RuntimeError('Live audit request budget exhausted')
            body=json.loads(request.content)
            if body.get("model") not in {"deepseek-v4-flash","deepseek-flash","deepseek-v4-pro"}:
                raise RuntimeError("Unpriced model blocked by audit cost guard")
            # UTF-8 bytes bound byte-level tokens; reserve template overhead too.
            local.ticket=budget.reserve(size+4096,body["max_tokens"])
            requests+=1;body_bytes+=size
    def hold_response(response):
        if cancellation_armed.is_set():
            request_started.set()
            if not request_release.wait(40):
                raise RuntimeError('Cancellation transport barrier timed out')
    def factory(config):
        provider=get_provider(config)
        if not hasattr(provider,'_client') or not hasattr(provider._client,'chat'):
            raise RuntimeError('BLOCKED: budgeted live harness requires an OpenAI-compatible configured provider')
        client=provider._client
        client.max_retries=0
        client._client.event_hooks['request'].append(guard)
        client._client.event_hooks['response'].append(hold_response)
        original=client.chat.completions.create
        def create(**kwargs):
            kwargs.update(max_tokens=16384,timeout=90)
            local.ticket=None
            try:
                result=original(**kwargs)
            except Exception as exc:
                if local.ticket is not None:
                    budget.settle(local.ticket,None,None)
                with lock:
                    response_metadata.append({'result':'error','error_type':type(exc).__name__})
                raise
            ticket=local.ticket
            if kwargs.get('stream'):
                def chunks():
                    pieces=[];finish_reason=None;stream_model=kwargs.get("model")
                    try:
                        for chunk in result:
                            usage=getattr(chunk,'usage',None)
                            if usage is not None:
                                budget.settle(ticket,usage.prompt_tokens,usage.completion_tokens)
                            choices=getattr(chunk,'choices',None) or []
                            if choices:
                                pieces.append(getattr(choices[0].delta,'content',None) or '')
                                finish_reason=choices[0].finish_reason or finish_reason
                            stream_model=getattr(chunk,'model',None) or stream_model
                            yield chunk
                    finally:
                        result.close()
                        budget.settle(ticket,None,None)
                        text=''.join(pieces)
                        with lock:
                            response_metadata.append({'result':'stream','model':stream_model,
                                'finish_reason':finish_reason,'content_chars':len(text),
                                'has_plan':'Plan:' in text,'has_tasklist':'TaskList:' in text,
                                'has_action':'Action:' in text,
                                'actions':sorted(set(re.findall(r'"action"\s*:\s*"([a-z_]+)"',text)))})
                return chunks()
            choices=getattr(result,'choices',None) or []
            choice=choices[0] if choices else None
            message=getattr(choice,'message',None)
            content=getattr(message,'content','') or ''
            usage=getattr(result,'usage',None)
            if usage is not None:
                budget.settle(ticket,usage.prompt_tokens,usage.completion_tokens)
            with lock:
                response_metadata.append({'result':type(result).__name__,
                    'model':getattr(result,'model',None),'finish_reason':getattr(choice,'finish_reason',None),'content_chars':len(content),
                    'has_plan':'Plan:' in content,'has_tasklist':'TaskList:' in content,
                    'has_action':'Action:' in content,
                    'prompt_tokens':getattr(usage,'prompt_tokens',None),
                    'completion_tokens':getattr(usage,'completion_tokens',None)})
            return result
        client.chat.completions.create=create
        providers.append(provider)
        return provider
    config=ProviderConfig(provider=saved['provider'],api_key=key,
        model_simple=saved.get('model_simple',''),model_complex=saved.get('model_complex',saved.get('model_simple','')),
        base_url=saved.get('base_url',''))
    import inspect,importlib.metadata
    distribution=importlib.metadata.distribution('openkyrozen')
    report={'installed_source':inspect.getsourcefile(build_application),
        'package_provenance':json.loads(distribution.read_text('direct_url.json') or '{}'),
        'provider':config.provider,'model':config.model_simple,'model_complex':config.model_complex,'output_token_limit':16384,'request_limit':limit,'checks':{}}
    try:
        provider=factory(config)
        if not focused:
            answer,_=provider.chat([{'role':'user','content':'Reply with exactly AUDIT_OK.'}])
            assert 'AUDIT_OK' in answer
            report['checks']['chat']='PASS'
            text=''.join(provider.chat_stream([{'role':'user','content':'Reply with exactly STREAM_OK.'}]))
            assert 'STREAM_OK' in text
            report['checks']['streaming']='PASS'
            with tempfile.TemporaryDirectory(prefix='openkyrozen-live-learning-') as directory:
                root=Path(directory)
                memory=build_memory(root/'state.sqlite3',user_id='audit',workspace_id='synthetic',session_id='learning')
                app=build_application(memory=memory,tools=ToolAdapters(root))
                runtime=app.runtime
                runtime._provider_config=config
                runtime.llm_provider=provider
                runtime._learning_model_response=lambda messages,**_:provider.chat(messages,model=config.model_simple)[0]
                runtime.learning_engine.registry=SkillRegistry(memory.store,workspace_id='synthetic',root=root/'skills')
                fact='User: Remember that Orion uses orion.toml for configuration.\nAssistant: Understood.'
                feature=None
                for _ in range(4):
                    memory.add_log(fact)
                    runtime.dispatch_learning_cycle(surface='audit',trigger='turn',max_features=1,
                        feature_names=('auto_learn_conversations',))
                    context=runtime._build_memory_context('Orion configuration orion.toml')
                    feature=next(item for item in runtime.learning_feature_status() if item['name']=='auto_learn_conversations')
                    if feature['last_product_id'] and feature['last_used_at'] and 'orion.toml' in context:
                        break
                assert feature['last_product_id'] and feature['last_used_at'],'No live learned product/use receipt'
                app.close()
                report['checks']['learning_product_reuse']='PASS'
        import subagent_workflow_acceptance as workflow
        if not cancellation_only:
            with patch('openkyrozen.security.credentials.decrypt_api_key',return_value=key), \
                 patch.object(workflow,'get_provider',factory), \
                 contextlib.redirect_stdout(io.StringIO()):
                workflow.acceptance(live=True)
            report['checks']['delegation_tools_and_independent_review']='PASS'
        # Run the same cancellation invariant with real transport, ensuring a
        # late provider result cannot write after cancellation.
        with tempfile.TemporaryDirectory(prefix='openkyrozen-live-cancel-') as directory:
            root=Path(directory);(root/'marker.txt').write_text('unchanged')
            app=build_application(memory=build_memory(root/'state.sqlite3'),tools=ToolAdapters(root))
            runtime=app.runtime;runtime._provider_config=config
            runtime.llm_provider=provider;runtime.get_provider=factory
            from openkyrozen.security.capabilities import issue_capability_token
            runtime._execution_capability_token=issue_capability_token('audit',frozenset({'read'}))
            runtime._surface_capabilities='read'
            runtime.set_interaction_mode('agent')
            cancellation_armed.set()
            run=runtime.delegation().spawn([workflow.assignment('marker.txt')])[0]
            try:
                assert request_started.wait(40), 'No in-flight provider request was observed'
                runtime.delegation().cancel(run['run_id'])
            finally:
                cancellation_armed.clear()
                request_release.set()
            runtime.delegation().wait([run['run_id']],45)
            assert runtime.delegation().detail(run['run_id'])['status']=='cancelled'
            assert (root/'marker.txt').read_text()=='unchanged'
            app.close()
            report['checks']['in_flight_response_cancellation']='PASS'
            report['checks']['write_suppression']='Covered by deterministic permitted-write regression; live assignment is read-only'
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL'
        detail=str(exc).replace(key,'[redacted]')
        try:
            runs=json.loads(detail)
            if isinstance(runs,list):
                detail=json.dumps([{'status':r.get('status'),'error':r.get('error'),
                    'profile':r.get('profile'),'assignment':r.get('assignment'),
                    'report_status':(r.get('report') or {}).get('status'),
                    'report_summary':(r.get('report') or {}).get('summary'),
                    'review_errors':[v.get('error') for v in r.get('reviews',[])]} for r in runs])
        except (ValueError,TypeError):
            pass
        report['error']=detail[:3000]
        if 'Live audit request budget exhausted' in detail or 'Live audit RMB cost budget exhausted' in detail:
            report['status']='BLOCKED'
    finally:
        for provider in providers:
            provider._client.close()
    from audit_identity import audit_identity
    report.update(requests=requests,input_bytes=body_bytes,cny_cost_upper_bound=budget.total,budget_ledger=str(budget.path),response_metadata=response_metadata,identity=audit_identity())
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--request-limit',type=int,default=50)
    parser.add_argument('--focused',action='store_true',help='Run only delegation and in-flight cancellation')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cancellation-only',action='store_true')
    parser.add_argument('--budget-ledger',type=Path,required=True)
    args=parser.parse_args()
    report=acceptance(min(args.request_limit,50),focused=args.focused or args.cancellation_only,budget_path=args.budget_ledger,cancellation_only=args.cancellation_only)
    args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    raise SystemExit(0 if report['status']=='PASS' else 1)
