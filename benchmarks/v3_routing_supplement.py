"""Separate R132 routing supplement; importing never reads credentials or sends requests."""
from __future__ import annotations

from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict, replace
from decimal import Decimal,InvalidOperation
import hashlib
from itertools import permutations
import json
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
    if variant not in VARIANTS:
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
        after = intervene(decision,variant,profile_override=override,interaction_mode=mode)
        guidance=[runtime._ponytail_context(item.profile) for item in (decision,after)]
        records.append({'index':len(records),'variant':variant,'before':asdict(decision),
            'after':asdict(after),'complexity':complexity,'profile_override':override,
            'interaction_mode':mode,'child':bool(runtime.execution_context.child_run_id),
            'system_one_backend':turn.fast_backend,'decision_assist_backend':'off','requests':[],
            'profile_guidance_hashes':[hashlib.sha256(text.encode()).hexdigest() for text in guidance]})
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


def validate_observations(records, variant):
    """Worker observations describe mechanism, never authorize a treatment."""
    from openkyrozen.routing.router import RoutingDecision
    if variant not in VARIANTS or not isinstance(records,list) or not records:
        return False
    requests=0
    try:
        for row in records:
            before=RoutingDecision(**row['before'])
            if (type(before.planning_required) is not bool or before.provider!='openai'
                    or before.model!='deepseek-flash' or before.reasoning!='standard'
                    or before.budget_tokens is not None or before.profile not in {'coder','researcher'}
                    or row['interaction_mode'] not in {'agent','ask','plan'}):
                return False
            expected=intervene(before,variant,profile_override=row['profile_override'],
                               interaction_mode=row['interaction_mode'])
            if (row['variant']!=variant or asdict(expected)!=row['after']
                    or row['system_one_backend']!='off' or row['decision_assist_backend']!='off'
                    or not isinstance(row['requests'],list)):
                return False
            for request in row['requests']:
                if (request['model']!='deepseek-flash' or request['reserve_tokens']!=4096
                        or not isinstance(request['message_projection_sha256'],str)
                        or len(request['message_projection_sha256'])!=64):
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


def analyze(rows):
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


def run(*,source,repo,core_output,core_budget,output,budget_path):
    """Explicit paid entry point; all preconditions run before credential access."""
    from benchmarks import v3_matched_live as live
    from benchmarks.v3_fixtures import FIXTURES
    from benchmarks.v3_matched import BudgetLedger,summarize
    from benchmarks.v3_deepseek import DEEPSEEK_PRICING,DEEPSEEK_RELAY_CONFIG,DEEPSEEK_ENDPOINT
    from benchmarks.v3_responses_relay import ResponsesRelay
    paths=[Path(item).resolve() for item in (source,repo,core_output,core_budget,output,budget_path)]
    source,repo,core_output,core_budget,output,budget_path=paths
    if (any(path.is_relative_to(source) for path in paths[2:]) or len(set(paths[2:]))!=4):
        raise ValueError('distinct external core and supplement report/ledger paths are required')
    candidate=live._source_snapshot(source)
    base_identity=live.apparatus_identity()
    fixtures={item['fixture_id']:item for item in FIXTURES if item['fixture_id'] in FIXTURE_IDS}
    identity=dict(base_identity)
    identity['cohort_sha256']=hashlib.sha256(live._canonical_bytes({
        'protocol':PROTOCOL,'fixtures':[fixtures[key] for key in FIXTURE_IDS],'schedule':schedule(),
        'analysis':{'resamples':20000,'seed':132,'lower_probability':.025}})).hexdigest()
    worker_script=live._require_dispatched_apparatus(source,base_identity)
    live._require_secure_evidence_io()
    with live._campaign_lock(core_output) as lease,live._campaign_lock(output):
        core,config,original_attempts,core_hash=_core_checkpoint(core_output,core_budget)
        summary=summarize(core.get('rows',[]),pricing=DEEPSEEK_PRICING)
        if (core.get('protocol')!='openkyrozen-v3-matched-live-v1' or core.get('candidate_sha')!=candidate
                or core.get('status')!='complete' or not summary['complete'] or not summary['accounting_complete']
                or any(core.get(key)!=value for key,value in base_identity.items())
                or not live._validate_rows_evidence(core_output,core['rows'],candidate,base_identity)
                or config[0]!='CNY' or config[2]!=DEEPSEEK_PRICING.sha256
                or not 0<config[1]<=15*10**12 or core.get('smoke_passed') is not True
                or core.get('relay_config')!=asdict(DEEPSEEK_RELAY_CONFIG)):
            raise ValueError('a complete same-candidate secure core campaign is required first')
        from benchmarks.v3_protected_worker import verify_worker_protection
        verify_worker_protection()
        binding={'protocol':PROTOCOL,'candidate_sha':candidate,'core_report_sha256':core_hash,
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
        budget=BudgetLedger(budget_path,limit_rmb=limit,pricing=DEEPSEEK_PRICING)
        if prior and json.loads(prior[0]).get('protocol')==PROTOCOL:
            report=live._load_checkpoint(output,budget_path)
            if (report.get('candidate_sha')!=candidate or report.get('origin')!=origin
                    or any(report.get(key)!=value for key,value in identity.items())):
                raise ValueError('supplement checkpoint identity changed')
            if report.get('status')=='blocked_safety_failure':
                raise ValueError('recorded supplement safety failure cannot be skipped')
        else:
            if output.exists() or set(by_id)!=original_ids or not prior or hashlib.sha256(prior[0].encode()).hexdigest()!=core_hash:
                raise ValueError('new supplement ledger is not the exact original backup')
            report={**identity,'protocol':PROTOCOL,'candidate_sha':candidate,'origin':origin,
                'schedule':schedule(),'rows':[],'calls':[],'status':'initialized',
                'relay_config':asdict(DEEPSEEK_RELAY_CONFIG),'model':DEEPSEEK_RELAY_CONFIG.model,
                'pricing':DEEPSEEK_PRICING.as_dict(),'model_benefit_identifiable':False}
        rows=report['rows']
        keys=[(row.get('fixture_id'),row.get('repeat'),row.get('variant')) for row in rows]
        expected={(row['fixture_id'],row['repeat'],'routing-'+row['variant']) for row in schedule()}
        if len(set(keys))!=len(keys) or any((f,r,'routing-'+v) not in expected for f,r,v in keys):
            raise ValueError('duplicate or foreign supplemental cells')
        calls=budget.campaign_calls()
        if any(call['attempt_id'] not in original_ids and ((call.get('fixture_id'),call.get('repeat'),call.get('arm')) not in expected
                or call.get('model')!='deepseek-flash' or call.get('max_output_tokens')!=4096) for call in calls):
            raise ValueError('supplement ledger contains a foreign attempt')
        for row in rows:
            row['evidence_valid']=False
            if row.get('worker_result_complete') is True:
                worker=live._validate_worker_evidence(output,row,candidate,identity,return_worker=True)
                row['evidence_valid']=isinstance(worker,dict)
                row['routing_evidence_valid']=validate_observations(worker.get('routing_observations'),row['variant']) if isinstance(worker,dict) else False
                row['routing_treatment_active']=active_treatment(worker.get('routing_observations'),row['variant']) if isinstance(worker,dict) else False
            journal=[call for call in calls if (call.get('fixture_id'),call.get('repeat'),call.get('arm'))==
                     (row['fixture_id'],row['repeat'],row['arm'])]
            if row.get('requests')!=len(journal) or row.get('attempts')!=len(journal):
                raise ValueError('supplement journal changed after its worker checkpoint; retain evidence and investigate')
            row['usage_complete']=bool(journal) and all(isinstance(call.get('usage'),dict) and
                call.get('status')=='settled' for call in journal)
            row['cost_cny']=str(sum(Decimal(call['actual_rmb']) for call in journal)) if row['usage_complete'] else None
        smoke={call['attempt_id']:call for call in calls if call['attempt_id'] in core.get('smoke_attempt_ids',[])}
        if len(smoke)!=2 or any(call.get('status')!='settled' or not isinstance(call.get('usage'),dict) for call in smoke.values()):
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
        report['summary']=analyze(rows)
        if report['summary']['complete']:
            checkpoint('complete')
            return report
        reservation=budget.cost(1_000_000,4096)
        if not budget.budget_ok or budget.remaining_rmb<reservation:
            checkpoint('blocked_budget_reservation')
            report['summary']=analyze(rows);checkpoint(report['status'])
            return report
        _,api_key=live._credential()
        relay=ResponsesRelay(endpoint=DEEPSEEK_ENDPOINT,api_key=api_key,budget=budget,config=DEEPSEEK_RELAY_CONFIG)
        relay.rows.extend(calls)
        relay.smoke_verified_candidate=candidate
        relay.smoke_verified_attempt_ids=tuple(core['smoke_attempt_ids'])
        with tempfile.TemporaryDirectory(prefix='openkyrozen-routing-supplement-',dir='/var/tmp') as directory:
            temporary_root=Path(directory);temporary_root.chmod(0o711)
            relay.start()
            try:
                for cell in schedule():
                    fixture_id,repeat,variant=(cell[key] for key in ('fixture_id','repeat','variant'))
                    previous=next((row for row in rows if (row['fixture_id'],row['repeat'],row['variant'])==(fixture_id,repeat,variant)),None)
                    if previous and live._row_can_resume_as_complete(previous,output,candidate,identity) and previous.get('routing_evidence_valid') is True:
                        continue
                    if previous:
                        report.setdefault('prior_rows',[]).append(dict(previous))
                        rows.remove(previous)
                    if budget.remaining_rmb<reservation:
                        checkpoint('blocked_budget_reservation');break
                    row=live._execute_cell(temporary_root=temporary_root,repeat=repeat,fixture=fixtures[fixture_id],
                        arm='routing-'+variant,arm_source=source,candidate=candidate,identity=identity,
                        worker_script=worker_script,relay=relay,model='deepseek-flash',output=output,
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
                    checkpoint(stop or ('blocked_provider_error' if relay.provider_error else
                        'blocked_budget_reservation' if relay.budget_blocked else 'running'))
                    if stop or relay.provider_error or relay.budget_blocked:break
                    if not budget.budget_ok:raise RuntimeError('budget overrun; stop all calls')
            finally:
                relay.close()
        report['summary']=analyze(rows)
        checkpoint(report['status'] if report['status']!='running' else
                   'complete' if report['summary']['complete'] else 'incomplete')
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
