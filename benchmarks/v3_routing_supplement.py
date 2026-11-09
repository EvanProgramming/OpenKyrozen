"""Separate R132 routing supplement; importing never reads credentials or sends requests."""
from __future__ import annotations

from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict, replace
from decimal import Decimal,InvalidOperation
import hashlib
from itertools import permutations
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import tempfile
from types import MethodType, SimpleNamespace

PROTOCOL = 'openkyrozen-v3-routing-supplement-v1'
FIXTURE_IDS = ('investigation-1','investigation-2','git_history-1',
               'refactoring-2','refactoring-3','implementation-3')
VARIANTS = ('default','planning-never','profile-fixed-researcher')
MODEL_PROTOCOL = 'openkyrozen-v3-model-routing-v1'
MODEL_VARIANTS = ('model-auto','model-simple')
MODEL_ANALYSIS = {'cluster':'fixture','resamples':20000,'seed':132,'lower_probability':.025,
    'quality':'both accepted and physical lower bounds >0',
    'matched_outcomes':'end-to-end latency per accepted task, including failures/retries',
    'exposure':'changed parent selected and served models; otherwise UNIDENTIFIABLE',
    'control':'original automatic_model callback returns configured simple; original reasoning derivation',
    'observation':'same pair of original decision calls in both arms',
    'monetary_savings_claimed':False}


def model_schedule():
    return [{'fixture_id':fixture,'repeat':repeat,'variant':variant}
        for index,fixture in enumerate(FIXTURE_IDS) for repeat in range(3)
        for variant in (MODEL_VARIANTS if (index+repeat)%2==0 else tuple(reversed(MODEL_VARIANTS)))]


def schedule():
    orders = tuple(permutations(sorted(VARIANTS)))
    return [{'fixture_id':fixture,'repeat':repeat,'variant':variant}
            for index,fixture in enumerate(FIXTURE_IDS) for repeat in range(3)
            for variant in orders[(index+2*repeat)%6]]


def intervene(decision, variant, *, profile_override, interaction_mode):
    if variant not in VARIANTS:
        raise ValueError('unknown frozen routing supplement variant')
    if variant == 'planning-never' and interaction_mode == 'agent':
        return replace(decision,planning_required=False)
    if (variant == 'profile-fixed-researcher' and profile_override == 'auto'
            and decision.profile in {'coder','researcher'}):
        return replace(decision,profile='researcher')
    return decision


def install(runtime, variant):
    """Observe/intervene at the original decision call, scoped to one real runtime."""
    if variant not in VARIANTS+MODEL_VARIANTS:
        raise ValueError('unknown frozen routing supplement variant')
    import openkyrozen.agent.preparation as preparation
    from openkyrozen.routing.settings import decision_assist_state
    if (runtime._interaction_controller.state().get('system_one_backend','off') != 'off'
            or decision_assist_state()['backend'] != 'off'):
        raise RuntimeError('routing supplement requires default-off optional backends')
    original_prepare, original_decide = runtime._prepare_turn, preparation.decide_request
    active = ContextVar('benchmark_routing_turn',default=None)
    records = []

    def decide(*args, **kwargs):
        decision,complexity = original_decide(*args,**kwargs)
        turn = active.get()
        if turn is None:
            return decision,complexity
        override = args[6] if len(args)>6 else kwargs['profile_override']
        mode = args[7] if len(args)>7 else kwargs['interaction_mode']
        model_control_applied=False
        model_fields={}
        if variant in MODEL_VARIANTS:
            config=args[0] if args else kwargs['config']
            advice=args[1] if len(args)>1 else kwargs['advice']
            eligible=(not runtime.execution_context.child_run_id and config is not None
                and config.model_main in {None,'','auto'} and advice.get('model') not in {'simple','reasoning'})
            selected_args,selected_kwargs=list(args),dict(kwargs)
            model_control_applied=variant=='model-simple' and eligible
            if model_control_applied:
                callback=lambda _text:config.model_simple
                if len(selected_args)>3:selected_args[3]=callback
                else:selected_kwargs['automatic_model']=callback
            after,second_complexity=original_decide(*selected_args,**selected_kwargs)
            if second_complexity!=complexity or any(getattr(after,key)!=getattr(decision,key)
                    for key in ('provider','budget_tokens','profile','planning_required')):
                raise RuntimeError('local routing observation changed non-model decisions')
            model_fields={'model_control_applied':model_control_applied,'model_control_eligible':eligible,
                'model_policy':{'simple':config.model_simple,'complex':config.model_complex,'main':config.model_main}
                    if config is not None else None,'advisory_model':advice.get('model')}
        else:
            after = intervene(decision,variant,profile_override=override,interaction_mode=mode)
        guidance=[runtime._ponytail_context(item.profile) for item in (decision,after)]
        records.append({'index':len(records),'variant':variant,'before':asdict(decision),
            'after':asdict(after),'complexity':complexity,'profile_override':override,
            'interaction_mode':mode,'child':bool(runtime.execution_context.child_run_id),
            'system_one_backend':turn.fast_backend,'decision_assist_backend':'off','requests':[],
            'profile_guidance_hashes':[hashlib.sha256(text.encode()).hexdigest() for text in guidance]})
        records[-1].update(model_fields)
        return after,complexity

    def prepare(self,turn):
        token=active.set(turn)
        try:
            original_prepare(turn)
            if records:
                records[-1].update(learning_profile=turn.learning_run.get('profile'),
                    learned_context_sha256=hashlib.sha256((turn.learned_context or '').encode()).hexdigest(),
                    learned_context_nonempty=bool(turn.learned_context),
                    capabilities=sorted(self._execution_capability_token.capabilities),
                    working_plan=self.tasks.working_plan_snapshot())
        finally:
            active.reset(token)

    def observe(record, tools):
        if not records:
            raise RuntimeError('provider invocation lacks its actual routing decision')
        entry=records[-1]
        entry['requests'].append({key:record.get(key) for key in
            ('kind','model','schema_sha256','message_projection_sha256','window_tokens','reserve_tokens')}
            | {'tool_names':sorted({item.get('name') or item.get('function',{}).get('name')
                                   for item in tools or [] if isinstance(item,dict)})})

    def close():
        runtime._prepare_turn=original_prepare
        if preparation.decide_request is decide:
            preparation.decide_request=original_decide
        if getattr(runtime,'_benchmark_routing_observer',None) is observe:
            del runtime._benchmark_routing_observer

    preparation.decide_request=decide
    runtime._prepare_turn=MethodType(prepare,runtime)
    runtime._benchmark_routing_observer=observe
    return SimpleNamespace(records=records,close=close)


def backup_ledger(core, target):
    """Consistent read-only source backup, preserving every original attempt row."""
    core,target=Path(core).resolve(),Path(target).resolve()
    if core==target or target.exists():
        raise ValueError('supplement requires a distinct new continuation ledger')
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(core.as_uri()+'?mode=ro',uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)


def validate_observations(records, variant, provider_policy=None):
    """Worker observations describe mechanism, never authorize a treatment."""
    from openkyrozen.routing.router import RoutingDecision
    if variant not in VARIANTS+MODEL_VARIANTS or not isinstance(records,list) or not records:
        return False
    requests=0
    try:
        for row in records:
            before=RoutingDecision(**row['before'])
            policy_valid = (before.provider == 'google' and before.model in provider_policy.role_models.values()
                            and before.reasoning in {'standard', 'extended'} if provider_policy is not None else
                            before.provider == 'openai' and before.model == 'deepseek-flash' and before.reasoning == 'standard')
            if (type(before.planning_required) is not bool or not policy_valid
                    or before.budget_tokens is not None or before.profile not in {'coder','researcher'}
                    or row['interaction_mode'] not in {'agent','ask','plan'}):
                return False
            if variant in MODEL_VARIANTS:
                policy=row['model_policy']
                if provider_policy is None or {key:policy[key] for key in ('simple','complex')}!=provider_policy.role_models:
                    return False
                eligible=(not row['child'] and policy['main'] in {None,'','auto'}
                          and row['advisory_model'] not in {'simple','reasoning'})
                controlled=variant=='model-simple' and eligible
                expected=replace(before,model=policy['simple'],reasoning='standard') if controlled else before
                if row['model_control_eligible'] is not eligible or row['model_control_applied'] is not controlled:
                    return False
            else:
                expected=intervene(before,variant,profile_override=row['profile_override'],
                                   interaction_mode=row['interaction_mode'])
            if (row['variant']!=variant or asdict(expected)!=row['after']
                    or row['system_one_backend']!='off' or row['decision_assist_backend']!='off'
                    or not isinstance(row['requests'],list)):
                return False
            for request in row['requests']:
                if ((request['model'] not in provider_policy.role_models.values() if provider_policy is not None else
                     request['model']!='deepseek-flash') or request['reserve_tokens']!=4096
                        or not isinstance(request['message_projection_sha256'],str)
                        or len(request['message_projection_sha256'])!=64):
                    return False
                if variant in MODEL_VARIANTS and request.get('kind')=='generation' and request['model']!=expected.model:
                    return False
                requests+=1
    except (KeyError,TypeError,ValueError):
        return False
    return requests>0


def active_treatment(records,variant):
    for row in records or []:
        requests=[item for item in row.get('requests',[]) if item.get('kind')=='generation']
        if row.get('before')==row.get('after') or not requests:
            continue
        if variant=='planning-never' and any(item.get('schema_sha256') is not None for item in requests):
            return True
        hashes=row.get('profile_guidance_hashes',[])
        if variant=='profile-fixed-researcher' and len(hashes)==2 and hashes[0]!=hashes[1]:
            return True
    return False


def _interval(effects):
    from benchmarks.v3_matched import _percentile
    rng=random.Random(132)
    samples=[sum(rng.choices(effects,k=6))/6 for _ in range(20_000)]
    return [float(_percentile(samples,.025)),float(_percentile(samples,.975))]


def analyze(rows, provider_policy=None):
    def known_cost(value):
        if not isinstance(value,str):return False
        try: amount=Decimal(value)
        except InvalidOperation:return False
        return amount.is_finite() and amount>=0
    expected={(row['fixture_id'],row['repeat'],row['variant']) for row in schedule()}
    keyed={(row.get('fixture_id'),row.get('repeat'),row.get('variant')):row for row in rows}
    complete=(len(rows)==len(keyed)==54 and set(keyed)==expected and all(
        all(row.get(field) is True for field in ('safety_passed','usage_complete','worker_result_complete',
                                                'evidence_valid','routing_evidence_valid'))
        and all(type(row.get(field)) is bool for field in ('physical_success','verified_success','workflow_state_correct'))
        and known_cost(row.get('cost_cny')) for row in rows))
    result={'complete':complete,'benefit_passed':False,'contrasts':{},'rows_observed':len(rows),
            'scope':'six frozen tasks; Flash/Flash; model-selection benefit not identifiable',
            'analysis':{'cluster':'fixture','resamples':20000,'seed':132,'one_sided_lower_probability':.025}}
    if provider_policy is not None:
        result['scope']='six frozen tasks; native Google automatic role selection; no separate model-selection intervention'
        result['provider_policy']=provider_policy.identity()
    if not complete:
        return result
    for control in VARIANTS[1:]:
        def accepted(row):return row['verified_success'] and row['workflow_state_correct']
        pairs=[(keyed[(fixture,r,'default')],keyed[(fixture,r,control)])
               for fixture in FIXTURE_IDS for r in range(3)]
        quality=[sum(int(accepted(left))-int(accepted(right)) for left,right in pairs[i:i+3])/3
                 for i in range(0,18,3)]
        physical=[sum(int(left['physical_success'])-int(right['physical_success']) for left,right in pairs[i:i+3])/3
                  for i in range(0,18,3)]
        interval=_interval(quality)
        item={'endpoint':'accepted_task_success','fixture_effects':quality,'interval':interval,
              'physical_interval':_interval(physical),'benefit_passed':False,
              'workflow_failures':sum(not row['workflow_state_correct'] for pair in pairs for row in pair),
              'requests':{arm:sum(row['requests'] for pair in pairs for row in (pair[arm],)) for arm in (0,1)},
              'input_tokens':{arm:sum(pair[arm]['input_tokens'] for pair in pairs) for arm in (0,1)},
              'output_tokens':{arm:sum(pair[arm]['output_tokens'] for pair in pairs) for arm in (0,1)},
              'cost_cny':{arm:str(sum(Decimal(pair[arm]['cost_cny']) for pair in pairs)) for arm in (0,1)},
              'latency_ms':{arm:sum(pair[arm]['latency_ms'] for pair in pairs) for arm in (0,1)}}
        active=any(row.get('routing_treatment_active') is True for _,row in pairs)
        matched=all(accepted(left)==accepted(right) and left['physical_success']==right['physical_success']
                    for left,right in pairs)
        if matched:
            costs=[]
            for index in range(0,18,3):
                group=pairs[index:index+3]
                counts=[sum(accepted(pair[arm]) for pair in group) for arm in (0,1)]
                if not all(counts):
                    break
                totals=[sum(Decimal(pair[arm]['cost_cny']) for pair in group)/counts[arm] for arm in (0,1)]
                costs.append(float(totals[1]-totals[0]))
            item.update(endpoint='cost_per_accepted_task',fixture_effects=costs,
                        interval=_interval(costs) if len(costs)==6 else None)
            item['benefit_passed']=bool(active and len(costs)==6 and item['interval'][0]>0)
        else:
            item['benefit_passed']=bool(active and interval[0]>0 and item['physical_interval'][0]>0)
        item['active_mechanism_observed']=active
        result['contrasts'][control]=item
    result['benefit_passed']=all(item['benefit_passed'] for item in result['contrasts'].values())
    return result


def model_analyze(rows, provider_policy):
    """Prospectively separate36 contrast; never changes the original54 endpoints."""
    from benchmarks.v3_gemini import served_identity
    expected={(row['fixture_id'],row['repeat'],row['variant']) for row in model_schedule()}
    keyed={(row.get('fixture_id'),row.get('repeat'),row.get('variant')):row for row in rows}
    result={'protocol':MODEL_PROTOCOL,'complete':False,'identifiable':False,'benefit_passed':False,
            'status':'INCOMPLETE','rows_observed':len(rows),'analysis':dict(MODEL_ANALYSIS),
            'monetary_savings_claimed':False,'endpoint':None,'scope':'deployed Auto vs configured simple only'}
    complete=(len(rows)==len(keyed)==36 and set(keyed)==expected and all(
        all(row.get(key) is True for key in ('safety_passed','usage_complete','worker_result_complete',
                                            'evidence_valid','routing_evidence_valid'))
        and all(type(row.get(key)) is bool for key in ('physical_success','verified_success','workflow_state_correct'))
        and type(row.get('latency_ms')) in {int,float} and math.isfinite(row['latency_ms']) and row['latency_ms']>0
        for row in rows))
    result['complete']=complete
    if not complete:return result
    pairs=[(keyed[(fixture,repeat,'model-auto')],keyed[(fixture,repeat,'model-simple')])
           for fixture in FIXTURE_IDS for repeat in range(3)]
    simple,complex_model=(provider_policy.role_models[key] for key in ('simple','complex'))
    identifiable=all(row.get('model_exposure',{}).get('complete') is True for row in rows)
    generation=[call for row in rows for call in row.get('model_exposure',{}).get('generation_calls',[])]
    ids=[call.get('attempt_id') for call in generation]
    identifiable=identifiable and all(isinstance(value,str) and value for value in ids) and len(set(ids))==len(ids)
    result['selected_role_fixture_coverage']={role:sorted({row['fixture_id'] for row in rows
        if row.get('model_exposure',{}).get('complete') is True and any(call.get('requested_model')==model
            and served_identity(model,{'modelVersion':call.get('served_model')})
            for call in row['model_exposure'].get('generation_calls',[]))})
        for role,model in provider_policy.role_models.items()}
    def exposed(row,model,controlled):
        return any(call.get('natural_model')==complex_model and call.get('selected_model')==model
            and call.get('requested_model')==model and call.get('control_applied') is controlled
            and served_identity(model,{'modelVersion':call.get('served_model')})
            for call in row.get('model_exposure',{}).get('generation_calls',[]))
    identifiable=identifiable and any(exposed(left,complex_model,False) and exposed(right,simple,True)
                                     for left,right in pairs)
    result.update(identifiable=identifiable,status='UNIDENTIFIABLE' if not identifiable else 'NO_BENEFIT')
    if not identifiable:return result
    accepted=lambda row:row['verified_success'] and row['workflow_state_correct']
    quality=[sum(int(accepted(left))-int(accepted(right)) for left,right in pairs[index:index+3])/3
             for index in range(0,18,3)]
    physical=[sum(int(left['physical_success'])-int(right['physical_success']) for left,right in pairs[index:index+3])/3
              for index in range(0,18,3)]
    result.update(accepted_fixture_effects=quality,physical_fixture_effects=physical,
                  accepted_interval=_interval(quality),physical_interval=_interval(physical),
                  latency_ms={arm:sum(pair[arm]['latency_ms'] for pair in pairs) for arm in (0,1)},
                  requests={arm:sum(pair[arm]['requests'] for pair in pairs) for arm in (0,1)})
    workflow_safe=all(not right['workflow_state_correct'] or left['workflow_state_correct'] for left,right in pairs)
    matched=all(accepted(left)==accepted(right) and left['physical_success']==right['physical_success']
                for left,right in pairs)
    if matched:
        effects=[]
        for index in range(0,18,3):
            group=pairs[index:index+3];counts=[sum(accepted(pair[arm]) for pair in group) for arm in (0,1)]
            if not all(counts):break
            totals=[sum(pair[arm]['latency_ms'] for pair in group)/counts[arm] for arm in (0,1)]
            effects.append(totals[1]-totals[0])
        result.update(endpoint='latency_per_accepted_task',fixture_effects=effects,
                      interval=_interval(effects) if len(effects)==6 else None)
        passed=workflow_safe and len(effects)==6 and result['interval'][0]>0
    else:
        result['endpoint']='accepted_task_success'
        passed=workflow_safe and result['accepted_interval'][0]>0 and result['physical_interval'][0]>0
    result.update(benefit_passed=bool(passed),status='BENEFIT' if passed else 'NO_BENEFIT')
    return result


def model_exposure(routing,ports,calls,provider_policy):
    """Strict join; ambiguous retries stay accounted but cannot prove treatment exposure."""
    from benchmarks.v3_gemini import served_identity
    result={'complete':False,'generation_calls':[],'reason':'ambiguous parent provider/physical lineage'}
    if not isinstance(routing,list) or not isinstance(ports,list) or not isinstance(calls,list):return result
    roots={call.get('actor_id') for call in calls if call.get('actor_role')=='root'}
    if len(roots)!=1 or None in roots:return result
    root=next(iter(roots))
    physical=[call for call in calls if call.get('actor_role')=='root' and call.get('actor_id')==root
              and call.get('root_actor_id')==root]
    if (not physical or any(type(call.get('admission_sequence')) is not int for call in physical)
            or len({call['admission_sequence'] for call in physical})!=len(physical)):
        return result
    physical.sort(key=lambda call:call['admission_sequence'])
    root_ports=[record for record in ports if 'actor_run_id' not in record]
    observed=[(decision,request) for decision in routing if decision.get('child') is False
              for request in decision.get('requests',[])]
    if not len(root_ports)==len(physical)==len(observed):return result
    joined=[]
    for port,call,(decision,request) in zip(root_ports,physical,observed):
        usage=call.get('usage')
        if (port.get('outcome')!='returned' or call.get('status')!='settled' or not isinstance(usage,dict)
                or port.get('model')!=call.get('model')
                or any(port.get(key)!=request.get(key) for key in ('kind','model','message_projection_sha256','schema_sha256','window_tokens','reserve_tokens'))
                or port.get('provider_input_tokens')!=usage.get('input_tokens')
                or port.get('provider_output_tokens')!=usage.get('output_tokens')
                or not served_identity(call['model'],{'modelVersion':usage.get('served_model')})):
            return result
        item={'attempt_id':call['attempt_id'],'actor_id':root,'admission_sequence':call['admission_sequence'],
              'kind':port['kind'],'natural_model':decision['before']['model'],
              'selected_model':decision['after']['model'],'requested_model':call['model'],
              'served_model':usage['served_model'],'control_applied':decision['model_control_applied'],
              'message_projection_sha256':port['message_projection_sha256'],'schema_sha256':port['schema_sha256'],
              'window_tokens':port['window_tokens'],'reserve_tokens':port['reserve_tokens']}
        if item['kind']=='generation' and item['selected_model']!=item['requested_model']:return result
        joined.append(item)
    if not any(item['kind']=='generation' for item in joined):return result
    return {'complete':True,'root_actor_id':root,'root_calls':joined,
            'generation_calls':[item for item in joined if item['kind']=='generation']}


def _core_checkpoint(output,budget_path):
    """Unlike the resumable writer helper, this cannot repair or mutate the core."""
    with closing(sqlite3.connect(Path(budget_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        checkpoint=db.execute('SELECT output_path,report_sha256,report_json FROM benchmark_checkpoint WHERE id=1').fetchone()
        config=db.execute('SELECT currency_code,limit_picormb,pricing_sha256 FROM budget_config WHERE id=1').fetchone()
        attempts=db.execute('SELECT * FROM attempts ORDER BY id').fetchall()
    if (not checkpoint or checkpoint[0]!=str(Path(output).resolve())
            or hashlib.sha256(checkpoint[2].encode()).hexdigest()!=checkpoint[1]
            or Path(output).read_bytes()!=checkpoint[2].encode()):
        raise ValueError('core canonical checkpoint must be intact; read-only handoff cannot repair it')
    return json.loads(checkpoint[2]),config,attempts,checkpoint[1]


def _bind_continuation(descriptor,binding):
    os.lseek(descriptor,0,os.SEEK_SET)
    existing=os.read(descriptor,65537)
    marker=json.loads(existing) if existing else None
    if marker and marker.get('phase') not in {'preparing','active'}:
        raise ValueError('invalid continuation activation phase')
    if marker and marker.get('binding')!=binding:
        raise ValueError('core is already bound to a different continuation; a new cap cannot be forked')
    if not existing:
        encoded=json.dumps({'binding':binding,'phase':'preparing'},sort_keys=True,separators=(',',':')).encode()
        os.lseek(descriptor,0,os.SEEK_SET)
        os.write(descriptor,encoded)
        os.fsync(descriptor)
    return marker['phase'] if marker else 'preparing'


def _activate_continuation(descriptor,binding):
    if _bind_continuation(descriptor,binding)=='active':
        return
    encoded=json.dumps({'binding':binding,'phase':'active'},sort_keys=True,separators=(',',':')).encode()
    os.lseek(descriptor,0,os.SEEK_SET)
    os.write(descriptor,encoded)
    os.ftruncate(descriptor,len(encoded))
    os.fsync(descriptor)


def _experiment_identity(base,fixtures,model_routing=False):
    from benchmarks.v3_matched_live import _canonical_bytes
    identity=dict(base)
    identity['cohort_sha256']=hashlib.sha256(_canonical_bytes({
        'protocol':MODEL_PROTOCOL if model_routing else PROTOCOL,
        'fixtures':[fixtures[key] for key in FIXTURE_IDS],
        'schedule':model_schedule() if model_routing else schedule(),
        'analysis':MODEL_ANALYSIS if model_routing else {'resamples':20000,'seed':132,'lower_probability':.025}})).hexdigest()
    return identity


def _attempt_calls(attempts):
    calls=[]
    for identifier,_reserved,actual,status,usage,metadata,*_ in attempts:
        value=json.loads(metadata or '{}')
        if value.get('archived_reservation'):continue
        calls.append(dict(value,attempt_id=identifier,status=status,usage=json.loads(usage) if usage else None,
                          actual_rmb=str(Decimal(actual)/Decimal(10**12)) if actual is not None else None))
    return calls


def _validate_54_origin(report,attempts,output,candidate,identity,provider_policy):
    from benchmarks import v3_matched_live as live
    if (report.get('protocol')!=PROTOCOL or report.get('candidate_sha')!=candidate or report.get('status')!='complete'
            or any(report.get(key)!=value for key,value in identity.items())
            or not analyze(report.get('rows',[]),provider_policy)['complete']
            or report.get('schedule')!=schedule() or report.get('pricing')!=provider_policy.pricing.as_dict()
            or not live._validate_rows_evidence(output,report['rows'],candidate,identity)):
        raise ValueError('model contrast requires complete same-candidate original54 evidence')
    calls=_attempt_calls(attempts)
    ancestors=set(report.get('origin',{}).get('original_attempt_ids',[]))
    expected={(cell['fixture_id'],cell['repeat'],'routing-'+cell['variant']) for cell in schedule()}
    if any(call['attempt_id'] not in ancestors and ((call.get('fixture_id'),call.get('repeat'),call.get('arm')) not in expected
            or not provider_policy.accepts_call(call)) for call in calls):
        raise ValueError('original54 contains a foreign physical attempt')
    for row in report['rows']:
        worker=live._validate_worker_evidence(output,row,candidate,identity,return_worker=True)
        journal=[call for call in calls if (call.get('fixture_id'),call.get('repeat'),call.get('arm'))==
                 (row['fixture_id'],row['repeat'],row['arm'])]
        if (not isinstance(worker,dict) or not validate_observations(worker.get('routing_observations'),row['variant'],provider_policy)
                or sorted(row.get('cost_calls',[]),key=lambda call:call['attempt_id'])!=sorted(journal,key=lambda call:call['attempt_id'])
                or row.get('requests')!=len(journal)
                or row.get('attempts')!=len(journal)):
            raise ValueError('original54 worker or physical journal lineage is invalid')
        if provider_policy.pricing.row_cost(row)!=Decimal(row['cost_cny']):
            raise ValueError('original54 model costs differ from its immutable ledger')


def run(*,source,repo,core_output,core_budget,output,budget_path,provider_policy=None,billing_proof=None,
        model_routing=False):
    """Explicit paid entry point; all preconditions run before credential access."""
    from benchmarks import v3_matched_live as live
    from benchmarks.v3_fixtures import FIXTURES
    from benchmarks.v3_matched import BudgetLedger,summarize
    from benchmarks.v3_deepseek import DEEPSEEK_PRICING,DEEPSEEK_RELAY_CONFIG,DEEPSEEK_ENDPOINT
    from benchmarks.v3_deepseek import validate_planning_preflight
    from benchmarks.v3_responses_relay import ResponsesRelay
    if type(model_routing) is not bool or model_routing and provider_policy is None:
        raise ValueError('model-routing phase requires its native two-role policy')
    protocol=MODEL_PROTOCOL if model_routing else PROTOCOL
    cells=model_schedule() if model_routing else schedule()
    analyze_phase=(lambda rows:model_analyze(rows,provider_policy)) if model_routing else (lambda rows:analyze(rows,provider_policy))
    pricing,relay_config,endpoint=DEEPSEEK_PRICING,DEEPSEEK_RELAY_CONFIG,DEEPSEEK_ENDPOINT
    if provider_policy is not None:
        from benchmarks.v3_gemini import GeminiPolicy,OFFICIAL_ENDPOINT,INPUT_BOUND
        from benchmarks.v3_responses_relay import RelayConfig
        if type(provider_policy) is not GeminiPolicy:
            raise ValueError('unsupported native supplement policy')
        pricing,relay_config,endpoint=provider_policy.pricing,RelayConfig('auto',4096,INPUT_BOUND,True),OFFICIAL_ENDPOINT
    paths=[Path(item).resolve() for item in (source,repo,core_output,core_budget,output,budget_path)]
    source,repo,core_output,core_budget,output,budget_path=paths
    if (any(path.is_relative_to(source) for path in paths[2:]) or len(set(paths[2:]))!=4):
        raise ValueError('distinct external core and supplement report/ledger paths are required')
    candidate=live._source_snapshot(source)
    base_identity=live.apparatus_identity()
    if provider_policy is not None:
        base_identity['provider_policy']=provider_policy.identity()
    fixtures={item['fixture_id']:item for item in FIXTURES if item['fixture_id'] in FIXTURE_IDS}
    identity=_experiment_identity(base_identity,fixtures,model_routing)
    worker_script=live._require_dispatched_apparatus(source,base_identity)
    live._require_secure_evidence_io()
    with live._campaign_lock(core_output) as lease,live._campaign_lock(output) as output_lease:
        os.lseek(output_lease,0,os.SEEK_SET)
        if os.read(output_lease,1):
            raise ValueError('supplement budget has a bound continuation; resume that child instead')
        core,config,original_attempts,core_hash=_core_checkpoint(core_output,core_budget)
        if provider_policy is not None and any(not provider_policy.accepts_call(call) for call in _attempt_calls(original_attempts)):
            raise ValueError('native physical origin call violates its operational policy')
        ancestor,ancestor_output,ancestor_attempts=core,core_output,original_attempts
        if model_routing:
            _validate_54_origin(core,original_attempts,core_output,candidate,
                                _experiment_identity(base_identity,fixtures),provider_policy)
            prior_origin=core.get('origin',{})
            if (not isinstance(prior_origin,dict) or any(not isinstance(prior_origin.get(key),str)
                    or not Path(prior_origin[key]).is_absolute() for key in ('core_output','core_budget'))):
                raise ValueError('original54 lacks its canonical180 origin')
            ancestor_output,ancestor_budget=(Path(prior_origin[key]).resolve() for key in ('core_output','core_budget'))
            with live._campaign_lock(ancestor_output) as ancestor_lease:
                ancestor,ancestor_config,ancestor_attempts,ancestor_hash=_core_checkpoint(ancestor_output,ancestor_budget)
                expected_binding={'protocol':PROTOCOL,'candidate_sha':candidate,'core_report_sha256':ancestor_hash,
                                  'output':str(core_output),'budget_path':str(core_budget)}
                os.lseek(ancestor_lease,0,os.SEEK_SET)
                marker=json.loads(os.read(ancestor_lease,65537))
                if (marker!={'binding':expected_binding,'phase':'active'} or ancestor_config!=config
                        or prior_origin['core_report_sha256']!=ancestor_hash
                        or not set(prior_origin['original_attempt_ids'])=={row[0] for row in ancestor_attempts}
                        or any(row not in original_attempts for row in ancestor_attempts)):
                    raise ValueError('model contrast lost its unique180 to54 ancestry')
        summary=summarize(ancestor.get('rows',[]),pricing=pricing)
        if (ancestor.get('protocol')!='openkyrozen-v3-matched-live-v1' or ancestor.get('candidate_sha')!=candidate
                or ancestor.get('status')!='complete' or not summary['complete'] or not summary['accounting_complete']
                or any(ancestor.get(key)!=value for key,value in base_identity.items())
                or not live._validate_rows_evidence(ancestor_output,ancestor['rows'],candidate,base_identity)
                or config[0]!='CNY' or config[2]!=pricing.sha256
                or not 0<config[1]<=15*10**12 or ancestor.get('smoke_passed') is not True
                or ancestor.get('relay_config')!=asdict(relay_config)):
            raise ValueError('a complete same-candidate secure core campaign is required first')
        preflight_calls=[]
        for identifier,reserved,actual,status,usage,metadata,*_rest in ancestor_attempts:
            call=json.loads(metadata or '{}')
            if call.get('arm')=='planning_preflight':
                quote=(provider_policy.pricing.for_metadata(call,input_tokens=INPUT_BOUND).cost(INPUT_BOUND,4096)
                       if provider_policy is not None else Decimal('2.032768'))
                if reserved!=int(quote*10**12):
                    raise ValueError('core planning preflight reservation changed')
                preflight_calls.append(dict(call,attempt_id=identifier,status=status,
                    usage=json.loads(usage) if usage else None,
                    actual_rmb=str(Decimal(actual)/Decimal(10**12)) if actual is not None else None))
        validate_planning_preflight(ancestor_output,ancestor.get('planning_preflight'),preflight_calls,candidate,base_identity,provider_policy)
        from benchmarks.v3_protected_worker import verify_worker_protection
        verify_worker_protection()
        binding={'protocol':protocol,'candidate_sha':candidate,'core_report_sha256':core_hash,
                 'output':str(output),'budget_path':str(budget_path)}
        phase=_bind_continuation(lease,binding)
        if not budget_path.exists():
            if phase=='active':
                raise ValueError('missing original continuation ledger; cannot recopy a fresh allowance')
            backup_ledger(core_budget,budget_path)
        with closing(sqlite3.connect(budget_path)) as db:
            target_rows=db.execute('SELECT * FROM attempts ORDER BY id').fetchall()
            prior=db.execute('SELECT report_json FROM benchmark_checkpoint WHERE id=1').fetchone()
        by_id={row[0]:row for row in target_rows}
        if any(by_id.get(row[0])!=row for row in original_attempts):
            raise ValueError('continuation changed an original attempt or unknown hold')
        original_ids={row[0] for row in original_attempts}
        origin={'core_report_sha256':core_hash,'core_output':str(core_output),'core_budget':str(core_budget),
                'original_attempt_ids':sorted(original_ids)}
        limit=str(Decimal(config[1])/Decimal(10**12))
        budget=BudgetLedger(budget_path,limit_rmb=limit,pricing=pricing)
        native_calls=budget.campaign_calls() if provider_policy is not None else None
        if native_calls is not None and any(not provider_policy.accepts_call(call) for call in native_calls):
            raise ValueError('native physical call violates its operational policy; retain the stopped checkpoint')
        if prior and json.loads(prior[0]).get('protocol')==protocol:
            report=live._load_checkpoint(output,budget_path)
            if (report.get('candidate_sha')!=candidate or report.get('origin')!=origin
                    or any(report.get(key)!=value for key,value in identity.items())):
                raise ValueError('supplement checkpoint identity changed')
            if report.get('status')=='blocked_safety_failure':
                raise ValueError('recorded supplement safety failure cannot be skipped')
            if model_routing and report.get('active_cell') is not None:
                raise ValueError('interrupted model cell cannot replay unmeasured end-to-end latency')
        else:
            if output.exists() or set(by_id)!=original_ids or not prior or hashlib.sha256(prior[0].encode()).hexdigest()!=core_hash:
                raise ValueError('new supplement ledger is not the exact original backup')
            report={**identity,'protocol':protocol,'candidate_sha':candidate,'origin':origin,
                'schedule':cells,'rows':[],'calls':[],'status':'initialized',
                'relay_config':asdict(relay_config),'model':relay_config.model,
                'pricing':pricing.as_dict(),'model_benefit_identifiable':False}
        rows=report['rows']
        keys=[(row.get('fixture_id'),row.get('repeat'),row.get('variant')) for row in rows]
        expected={(row['fixture_id'],row['repeat'],'routing-'+row['variant']) for row in cells}
        if len(set(keys))!=len(keys) or any((f,r,'routing-'+v) not in expected for f,r,v in keys):
            raise ValueError('duplicate or foreign supplemental cells')
        calls=native_calls if native_calls is not None else budget.campaign_calls()
        if any(call['attempt_id'] not in original_ids and ((call.get('fixture_id'),call.get('repeat'),call.get('arm')) not in expected
                or (not provider_policy.accepts_call(call) if provider_policy is not None else
                    call.get('model')!='deepseek-flash' or call.get('max_output_tokens')!=4096)) for call in calls):
            raise ValueError('supplement ledger contains a foreign attempt')
        for row in rows:
            row['evidence_valid']=False
            if row.get('worker_result_complete') is True:
                worker=live._validate_worker_evidence(output,row,candidate,identity,return_worker=True)
                row['evidence_valid']=isinstance(worker,dict)
                row['routing_evidence_valid']=validate_observations(worker.get('routing_observations'),row['variant'],provider_policy) if isinstance(worker,dict) else False
                row['routing_treatment_active']=active_treatment(worker.get('routing_observations'),row['variant']) if isinstance(worker,dict) else False
            journal=[call for call in calls if (call.get('fixture_id'),call.get('repeat'),call.get('arm'))==
                     (row['fixture_id'],row['repeat'],row['arm'])]
            if row.get('requests')!=len(journal) or row.get('attempts')!=len(journal):
                raise ValueError('supplement journal changed after its worker checkpoint; retain evidence and investigate')
            row['usage_complete']=bool(journal) and all(isinstance(call.get('usage'),dict) and
                call.get('status')=='settled' for call in journal)
            row['cost_cny']=str(sum(Decimal(call['actual_rmb']) for call in journal)) if row['usage_complete'] else None
            if provider_policy is not None:
                if row.get('cost_calls') != journal:
                    raise ValueError('supplement native costs differ from original physical calls')
                if row['usage_complete'] and pricing.row_cost(row)!=Decimal(row['cost_cny']):
                    raise ValueError('supplement native model pricing changed')
            if model_routing and row.get('worker_result_complete') is True:
                exposure=model_exposure(worker.get('routing_observations'),worker.get('request_context_records'),journal,provider_policy)
                if row.get('model_exposure')!=exposure:
                    raise ValueError('model-routing exposure differs from its immutable worker and physical ledger')
        smoke={call['attempt_id']:call for call in calls if call['attempt_id'] in ancestor.get('smoke_attempt_ids',[])}
        if len(smoke)!=(provider_policy.smoke_calls if provider_policy is not None else 2) or any(call.get('status')!='settled' or not isinstance(call.get('usage'),dict) for call in smoke.values()):
            raise ValueError('core native Responses smoke is not fully accounted')
        def checkpoint(status):
            report.update(status=status,rows=rows,
                calls=[{key:value for key,value in call.items() if key!='response_body'} for call in calls],
                held_reservations=live._held_reservations(calls),
                budget={'currency':'CNY','limit_cny':limit,'spent_cny':str(budget.spent_rmb),
                        'remaining_cny':str(budget.remaining_rmb)})
            live._write_checkpoint(output,budget_path,report)
        checkpoint('running')
        # Only a preparing ledger may be recovered from an interrupted backup.
        # Once activated, deletion cannot reset exposure or hide a safety stop.
        _activate_continuation(lease,binding)
        report['summary']=analyze_phase(rows)
        if model_routing:report['model_benefit_identifiable']=report['summary']['identifiable']
        if report['summary']['complete']:
            checkpoint('unidentifiable' if model_routing and not report['summary']['identifiable'] else 'complete')
            return report
        reservation=(max(pricing.for_metadata(provider_policy.metadata(model),input_tokens=INPUT_BOUND).cost(INPUT_BOUND,4096)
                         for model in provider_policy.role_models.values()) if provider_policy is not None else budget.cost(1_000_000,4096))
        if not budget.budget_ok or budget.remaining_rmb<reservation:
            checkpoint('blocked_budget_reservation')
            report['summary']=analyze_phase(rows);checkpoint(report['status'])
            return report
        if provider_policy is not None:
            provider_policy.check_budget(budget)
            billing=provider_policy.verify_billing(billing_proof,candidate)
            if billing!=ancestor.get('billing_proof'):
                raise ValueError('supplement account proof differs from its core')
            if any(not provider_policy.accepts_call(call) for call in calls):
                raise ValueError('supplement physical calls differ from their signed account proof')
            _,api_key=provider_policy.credential()
            from benchmarks.v3_gemini import GeminiRelay
            relay=GeminiRelay(endpoint=endpoint,api_key=api_key,budget=budget,policy=provider_policy)
        else:
            _,api_key=live._credential()
            relay=ResponsesRelay(endpoint=endpoint,api_key=api_key,budget=budget,config=relay_config)
        relay.rows.extend(calls)
        relay.smoke_verified_candidate=candidate
        relay.smoke_verified_attempt_ids=tuple(ancestor['smoke_attempt_ids'])
        with tempfile.TemporaryDirectory(prefix='openkyrozen-routing-supplement-',dir='/var/tmp') as directory:
            temporary_root=Path(directory);temporary_root.chmod(0o711)
            relay.start()
            try:
                for cell in cells:
                    fixture_id,repeat,variant=(cell[key] for key in ('fixture_id','repeat','variant'))
                    previous=next((row for row in rows if (row['fixture_id'],row['repeat'],row['variant'])==(fixture_id,repeat,variant)),None)
                    if previous and live._row_can_resume_as_complete(previous,output,candidate,identity) and previous.get('routing_evidence_valid') is True:
                        continue
                    if previous:
                        report.setdefault('prior_rows',[]).append(dict(previous))
                        rows.remove(previous)
                    if budget.remaining_rmb<reservation:
                        checkpoint('blocked_budget_reservation');break
                    if model_routing:
                        report['active_cell']=dict(cell)
                        checkpoint('running')
                    row=live._execute_cell(temporary_root=temporary_root,repeat=repeat,fixture=fixtures[fixture_id],
                        arm='routing-'+variant,arm_source=source,candidate=candidate,identity=identity,
                        worker_script=worker_script,relay=relay,model=relay_config.model,output=output,
                        api_key=api_key,budget=budget,routing_variant=variant)
                    calls=relay.rows
                    cell_calls=[call for call in calls if (call.get('fixture_id'),call.get('repeat'),call.get('arm'))==
                                (fixture_id,repeat,'routing-'+variant)]
                    row['cost_cny']=(str(sum(Decimal(call['actual_rmb']) for call in cell_calls))
                                     if cell_calls and all(call.get('status')=='settled' and call.get('actual_rmb') is not None for call in cell_calls) else None)
                    row['evidence_valid']=live._validate_worker_evidence(output,row,candidate,identity)
                    rows.append(row)
                    stop=live._safety_failure_status(row)
                    if not row.get('routing_evidence_valid') or not row['evidence_valid']:
                        stop='blocked_safety_failure'
                    if model_routing:report['active_cell']=None
                    checkpoint(stop or ('blocked_provider_error' if relay.provider_error else
                        'blocked_budget_reservation' if relay.budget_blocked else 'running'))
                    if stop or relay.provider_error or relay.budget_blocked:break
                    if not budget.budget_ok:raise RuntimeError('budget overrun; stop all calls')
            finally:
                relay.close()
        report['summary']=analyze_phase(rows)
        if model_routing:report['model_benefit_identifiable']=report['summary']['identifiable']
        checkpoint(report['status'] if report['status']!='running' else
                   ('unidentifiable' if model_routing and not report['summary']['identifiable'] else 'complete')
                   if report['summary']['complete'] else 'incomplete')
        return report


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','repo','core-output','core-budget','output','budget-path'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--execute',action='store_true',help='Explicitly permit metered provider calls')
    args=parser.parse_args()
    if not args.execute:parser.error('--execute is required; offline import/testing makes no provider calls')
    from scripts.publish_v3_preview import _verify_candidate_signature
    from benchmarks.v3_matched_live import _source_snapshot
    _verify_candidate_signature(args.source,_source_snapshot(args.source))
    report=run(source=args.source,repo=args.repo,core_output=args.core_output,core_budget=args.core_budget,
               output=args.output,budget_path=args.budget_path)
    print(json.dumps(report['summary'],indent=2,sort_keys=True))
    raise SystemExit(0 if report['summary']['benefit_passed'] else 1)


if __name__=='__main__':main()
