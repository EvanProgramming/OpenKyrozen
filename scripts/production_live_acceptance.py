#!/usr/bin/env python3
"""Bounded real-provider checks using synthetic content and read-only credentials."""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]


def acceptance(limit):
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
    requests=body_bytes=0
    providers=[]
    def guard(request):
        nonlocal requests,body_bytes
        size=len(request.content)
        with lock:
            if requests>=limit or size>100000 or body_bytes+size>1000000:
                raise RuntimeError('Live audit request budget exhausted')
            requests+=1;body_bytes+=size
    def factory(config):
        config.model_complex=config.model_simple
        provider=get_provider(config)
        if not hasattr(provider,'_client') or not hasattr(provider._client,'chat'):
            raise RuntimeError('BLOCKED: budgeted live harness requires an OpenAI-compatible configured provider')
        client=provider._client
        client.max_retries=0
        client._client.event_hooks['request'].append(guard)
        original=client.chat.completions.create
        def create(**kwargs):
            kwargs.update(max_tokens=2048,timeout=40)
            return original(**kwargs)
        client.chat.completions.create=create
        providers.append(provider)
        return provider
    config=ProviderConfig(provider=saved['provider'],api_key=key,
        model_simple=saved.get('model_simple',''),model_complex=saved.get('model_simple',''),
        base_url=saved.get('base_url',''))
    report={'provider':config.provider,'model':config.model_simple,'request_limit':limit,'checks':{}}
    try:
        provider=factory(config)
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
            run=runtime.delegation().spawn([workflow.assignment('marker.txt')])[0]
            runtime.delegation().cancel(run['run_id'])
            runtime.delegation().wait([run['run_id']],45)
            assert runtime.delegation().detail(run['run_id'])['status']=='cancelled'
            assert (root/'marker.txt').read_text()=='unchanged'
            app.close()
            report['checks']['cancellation']='PASS'
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL'
        detail=str(exc).replace(key,'[redacted]')
        try:
            runs=json.loads(detail)
            if isinstance(runs,list):
                detail=json.dumps([{'status':r.get('status'),'error':r.get('error'),
                    'review_errors':[v.get('error') for v in r.get('reviews',[])]} for r in runs])
        except (ValueError,TypeError):
            pass
        report['error']=detail[:3000]
    finally:
        for provider in providers:
            provider._client.close()
    report.update(requests=requests,input_bytes=body_bytes)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--request-limit',type=int,default=50)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=acceptance(min(args.request_limit,50))
    args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    raise SystemExit(0 if report['status']=='PASS' else 1)
