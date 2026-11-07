"""Offline mechanism/protocol checks; no provider or credential access."""
import copy
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def supplement():
    return importlib.import_module('benchmarks.v3_routing_supplement')


class RoutingProtocolTests(unittest.TestCase):
    def test_schedule_is_the_frozen_balanced_54_cells_without_modifying_core(self):
        from benchmarks.v3_fixtures import FIXTURES, cohort_digest
        module = supplement()
        original = copy.deepcopy(FIXTURES), cohort_digest()
        schedule = module.schedule()
        self.assertEqual(len(schedule), 54)
        self.assertEqual(len({(row['fixture_id'], row['repeat'], row['variant']) for row in schedule}), 54)
        self.assertEqual([row['fixture_id'] for row in schedule[::9]], list(module.FIXTURE_IDS))
        from collections import Counter
        orders = Counter(tuple(row['variant'] for row in schedule[index:index+3])
                         for index in range(0,54,3))
        self.assertEqual(sorted(orders.values()), [3]*6)
        self.assertEqual((FIXTURES,cohort_digest()), original)

    def test_only_one_resolved_field_changes_and_explicit_roles_and_modes_are_preserved(self):
        from openkyrozen.routing.router import RoutingDecision
        module = supplement()
        before = RoutingDecision('openai','deepseek-flash','standard',None,'coder',True)
        for variant, expected in [('default',{}),('planning-never',{'planning_required':False}),
                                  ('profile-fixed-researcher',{'profile':'researcher'})]:
            after = module.intervene(before, variant, profile_override='auto', interaction_mode='agent')
            self.assertEqual(asdict(after), dict(asdict(before), **expected))
        for role in ('coder','researcher','reviewer','custom-role',None):
            self.assertEqual(module.intervene(before,'profile-fixed-researcher',profile_override=role,
                interaction_mode='agent'), before)
        for mode in ('ask','plan'):
            decision = type(before)(**dict(asdict(before),planning_required=False))
            self.assertEqual(module.intervene(decision,'planning-never',profile_override='auto',
                interaction_mode=mode), decision)
        with self.assertRaises(ValueError):
            module.intervene(before,'planning-always',profile_override='auto',interaction_mode='agent')

    def test_analysis_never_turns_labels_or_incomplete_cells_into_a_benefit(self):
        module = supplement()
        self.assertFalse(module.analyze([])['complete'])
        rows = [dict(row, physical_success=True, verified_success=True, workflow_state_correct=True,
                     safety_passed=True, usage_complete=True, worker_result_complete=True,
                     evidence_valid=True, routing_evidence_valid=True, cost_cny='0.01',requests=1,
                     input_tokens=100,output_tokens=20,latency_ms=10,
                     routing_treatment_active=row['variant']!='default') for row in module.schedule()]
        equal = module.analyze(rows)
        self.assertTrue(equal['complete'])
        self.assertFalse(equal['benefit_passed'])
        for row in rows:
            if row['variant']=='default': row['cost_cny']='0.005'
        efficient = module.analyze(rows)
        self.assertTrue(efficient['benefit_passed'])
        self.assertTrue(all(item['endpoint']=='cost_per_accepted_task' for item in efficient['contrasts'].values()))
        rows[0]['routing_evidence_valid']=False
        self.assertFalse(module.analyze(rows)['benefit_passed'])

    def test_fixture_cluster_interval_preserves_all_three_repeats_and_receipt_only_gain_is_rejected(self):
        module=supplement()
        rows=[dict(row,physical_success=True,verified_success=True,workflow_state_correct=True,
                   safety_passed=True,usage_complete=True,worker_result_complete=True,evidence_valid=True,
                   routing_evidence_valid=True,routing_treatment_active=row['variant']!='default',
                   cost_cny='0.01',requests=1,input_tokens=100,output_tokens=20,latency_ms=10)
              for row in module.schedule()]
        for row in rows:
            if row['variant']!='default' and row['fixture_id'] in module.FIXTURE_IDS[:3]:
                row['physical_success']=row['verified_success']=False
        result=module.analyze(rows)
        for contrast in result['contrasts'].values():
            self.assertEqual(contrast['endpoint'],'accepted_task_success')
            self.assertEqual(contrast['fixture_effects'],[1,1,1,0,0,0])
            self.assertAlmostEqual(contrast['interval'][0],1/6)
            self.assertAlmostEqual(contrast['interval'][1],5/6)
        for row in rows:row['physical_success']=True
        self.assertFalse(module.analyze(rows)['benefit_passed'])
        rows[0]['cost_cny']='unavailable'
        self.assertFalse(module.analyze(rows)['complete'])

    def test_summary_cost_totals_include_failed_work_and_keep_fixture_balanced_denominators(self):
        module=supplement()
        rows=[]
        for cell in module.schedule():
            accepted=cell['fixture_id']!=module.FIXTURE_IDS[0] or cell['repeat']==0
            rows.append(dict(cell,physical_success=accepted,verified_success=accepted,
                workflow_state_correct=True,safety_passed=True,usage_complete=True,
                worker_result_complete=True,evidence_valid=True,routing_evidence_valid=True,
                routing_treatment_active=cell['variant']!='default',requests=1,input_tokens=100,
                output_tokens=20,latency_ms=10,
                cost_cny='1.1' if cell['variant']!='default' and cell['repeat']==1 else '0.1'))
        result=module.analyze(rows)
        self.assertTrue(result['complete'])
        for contrast in result['contrasts'].values():
            self.assertEqual(contrast['cost_cny'],{0:'1.8',1:'7.8'})
            self.assertEqual(contrast['endpoint'],'cost_per_accepted_task')
            self.assertAlmostEqual(contrast['fixture_effects'][0],1)
            self.assertTrue(all(abs(effect-1/3)<1e-12 for effect in contrast['fixture_effects'][1:]))
            mean=sum(contrast['fixture_effects'])/6
            self.assertAlmostEqual(mean,4/9)
            self.assertNotAlmostEqual(mean,6/16)  # pooled cost / pooled successes is a different estimand

    def test_parent_routing_rpc_cannot_be_selected_by_claimed_child_payload(self):
        from benchmarks.v3_actor_supervisor import _ActorBroker
        broker=_ActorBroker.__new__(_ActorBroker)
        broker.supervisor=SimpleNamespace(routing_variant='planning-never')
        broker.actor={'root':False,'assignment':SimpleNamespace(payload={'routing_variant':'profile-fixed-researcher'}),
                      'provider_config':{}}
        self.assertEqual(broker._dispatch({'op':'routing_policy'}),{'variant':'planning-never'})
        self.assertEqual(broker._dispatch({'op':'actor_bootstrap'})['routing_variant'],'planning-never')
        with self.assertRaises(PermissionError):
            broker._dispatch({'op':'routing_policy','variant':'default'})

    def test_readonly_ledger_backup_retains_attempt_ids_costs_holds_and_cap(self):
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING
        from benchmarks.v3_matched import BudgetLedger
        module = supplement()
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, target = root/'core.sqlite3', root/'supp.sqlite3'
            ledger = BudgetLedger(core,limit_rmb='15',pricing=DEEPSEEK_PRICING)
            for identifier in ('settled','unknown'):
                ledger.reserve(identifier,input_token_bound=1_000_000,output_token_bound=4096,
                    metadata={'reserved_rmb':'2.032768','arm':'v3','fixture_id':'fixture','repeat':0})
            ledger.settle('settled',{'input_tokens':100,'output_tokens':10,'cache_read_tokens':0,'cache_write_tokens':0})
            ledger.settle('unknown',None)
            before = core.read_bytes()
            module.backup_ledger(core,target)
            copied = BudgetLedger(target,limit_rmb='15',pricing=DEEPSEEK_PRICING)
            self.assertEqual(copied.remaining_rmb,ledger.remaining_rmb)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT id,status FROM attempts ORDER BY id').fetchall(),
                                 [('settled','settled'),('unknown','unknown')])
            self.assertEqual(core.read_bytes(),before)
            with self.assertRaises(ValueError): module.backup_ledger(core,target)

    def test_core_and_supplement_share_an_exclusive_canonical_report_lease(self):
        from benchmarks.v3_matched_live import _campaign_lock
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'core.json'
            report.write_text('{}')
            alias=Path(directory)/'alias.json';alias.symlink_to(report)
            with _campaign_lock(report):
                with self.assertRaisesRegex(ValueError,'active'):
                    with _campaign_lock(report): self.fail('concurrent campaign entered')
                with self.assertRaisesRegex(ValueError,'active'):
                    with _campaign_lock(alias): self.fail('alias bypassed canonical report lease')

    def test_one_core_continuation_cannot_be_forked_to_a_new_budget(self):
        from benchmarks.v3_matched_live import _campaign_lock
        module=supplement()
        with tempfile.TemporaryDirectory() as directory:
            with _campaign_lock(Path(directory)/'core.json') as descriptor:
                module._bind_continuation(descriptor,{'budget':'one'})
                module._bind_continuation(descriptor,{'budget':'one'})
                with self.assertRaisesRegex(ValueError,'different continuation'):
                    module._bind_continuation(descriptor,{'budget':'two'})

    def test_report_variant_label_never_changes_actual_v3_worker_abi_or_secure_retention(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_fixtures import FIXTURES
        fixture=next(row for row in FIXTURES if row['fixture_id']=='investigation-1')
        relay=SimpleNamespace(label=None,url='http://localhost',client_key='local-token',actor_keys=(),rows=[],
            issue_actor_config=lambda actor:{},revoke_actor=lambda actor:None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(live,'_run_worker',return_value=(-1,'blocked')) as execute, \
             patch.object(live,'_read_worker_result',return_value=({}, {'classification':'missing'})), \
             patch.object(live,'_write_worker_evidence') as retained:
            source=Path(__file__).resolve().parents[1]
            row=live._execute_cell(temporary_root=Path(directory),repeat=0,fixture=fixture,
                arm='routing-planning-never',arm_source=source,candidate='candidate',identity={
                    'cohort_sha256':'cohort','apparatus_sha256':'apparatus'},worker_script=source/'benchmarks/prompt_worker.py',
                relay=relay,model='deepseek-flash',output=Path(directory)/'report.json',api_key='offline',
                budget=SimpleNamespace(budget_ok=True),routing_variant='planning-never')
            command=execute.call_args.args[0]
            self.assertEqual(command[command.index('--source')+1],str(source))
            self.assertEqual(command[command.index('--protocol')+1],'responses')
            self.assertEqual(execute.call_args.kwargs['routing_variant'],'planning-never')
            self.assertEqual(row['arm'],'routing-planning-never')
            self.assertEqual(row['variant'],'planning-never')
            self.assertFalse(row['worker_result_complete'])
            retained.assert_called_once()

    def test_physical_outcome_is_separate_from_required_plan_reordering(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_fixtures import FIXTURES
        fixture=next(row for row in FIXTURES if row['fixture_id']=='implementation-3')
        with tempfile.TemporaryDirectory() as directory, patch.object(live,'_run_fixture_tests',return_value=True):
            root=Path(directory);prepared=live.prepare_fixture(root,fixture)
            for name in ('flags.py','consumer.py'):
                (root/name).write_text((root/name).read_text()+'\n# verified production change\n')
            verified=live.verify_fixture(fixture,root,'completed',{'provider_error':False,'processes_alive':0},prepared)
            self.assertFalse(verified['plan_scenario_passed'])
            self.assertTrue(verified['physical_success'])
            self.assertTrue(verified['verified_success'])
            self.assertFalse(live._workflow_state_correct(fixture,{}))


class RoutingRuntimeTests(unittest.TestCase):
    def runtime(self):
        from openkyrozen.app.bootstrap import build_application,build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.providers import ProviderConfig
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        workspace = root/'workspace';workspace.mkdir()
        env = patch.dict(os.environ,{'HOME':str(root),'KYROZEN_WORKSPACE_ROOT':str(workspace),
            'KYROZEN_DB_PATH':str(root/'state.sqlite3'),'KYROZEN_SKILLS_DIR':str(root/'skills'),
            'KYROZEN_DISABLE_VECTOR_INDEX':'1','KYROZEN_DECISION_ASSIST_BACKEND':'off'})
        env.start();self.addCleanup(env.stop)
        app = build_application(surface='cli',memory=build_memory(root/'state.sqlite3',index=None),
                                tools=ToolAdapters(workspace))
        self.addCleanup(app.close)
        runtime=app.runtime;runtime._set_workspace_root(workspace);runtime.set_interaction_mode('agent')
        runtime._provider_config=ProviderConfig(provider='openai',api_key='offline',
            model_simple='deepseek-flash',model_complex='deepseek-flash',context_window_tokens=1000000)
        runtime.dispatch_learning_cycle=lambda **kwargs:None
        runtime._plugin_runtime_for_surface=lambda *args:SimpleNamespace(
            turn_start=lambda **kwargs:None,turn_end=lambda **kwargs:None)
        return runtime

    def test_real_preparation_and_native_engine_observe_plan_schema_intervention(self):
        from benchmarks.prompt_worker import _ProviderObservations
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.models import ModelResponse,FinishReason,ProviderCapabilities,provider_continuation
        schemas=[]
        class Provider(LLMProvider):
            def get_capabilities(self,model=None):return ProviderCapabilities(native_tools=True)
            def chat(self,messages,model=None):return self.chat_response(messages,model).as_legacy_tuple()
            def chat_response(self,messages,model=None,*,tools=None):
                schemas.append({row.get('name') or row.get('function',{}).get('name') for row in tools or []})
                return ModelResponse(text='Offline final.',finish_reason=FinishReason.FINAL,
                    continuation=provider_continuation(self,model,'fixture',{'assistant':{'role':'assistant','content':'Offline final.'}}))
        observed=[]
        for variant in ('default','planning-never'):
            runtime=self.runtime();runtime.MAX_STEPS_PER_TURN=1
            provider=Provider(runtime._provider_config);runtime.llm_provider=provider;runtime._default_turn.provider=provider
            observer=_ProviderObservations(runtime,provider)
            controller=supplement().install(runtime,variant)
            try:
                runtime.chat(runtime.current_session,'Refactor the implementation in source.py.',profile='auto')
                observed.append((copy.deepcopy(controller.records),schemas[-1]))
                self.assertTrue(observer.records)
                self.assertEqual(observer.records[0]['model'],'deepseek-flash')
                self.assertTrue(controller.records[0]['requests'])
            finally:controller.close()
        self.assertEqual(observed[0][1],{'update_plan'})
        self.assertGreater(len(observed[1][1]),1)
        self.assertTrue(observed[0][0][0]['before']['planning_required'])
        self.assertFalse(observed[1][0][0]['after']['planning_required'])

    def test_real_preparation_preserves_child_role_and_accepted_plan_and_changes_cold_profile_context(self):
        from openkyrozen.agent.turn import TurnContext
        runtime=self.runtime()
        runtime.tasks.edit_plan(complexity='complex',expected_revision=0,operations=[{'op':'add','ref':'one',
            'task':{'description':'Keep seeded task','acceptance':['preserve']}}],reason='Frozen seed')
        initial=runtime.tasks.working_plan_snapshot()
        controller=supplement().install(runtime,'profile-fixed-researcher')
        try:
            turn=TurnContext('Inspect catalog.py and report which function sorts products and its exact tie-break rule.',profile='auto')
            runtime._prepare_turn(turn)
            record=controller.records[-1]
            self.assertEqual(record['before']['profile'],'coder')
            self.assertEqual(record['after']['profile'],'researcher')
            self.assertNotEqual(*record['profile_guidance_hashes'])
            self.assertEqual(runtime.tasks.working_plan_snapshot(),initial)
            runtime.execution_context.child_run_id='bound-child'
            explicit=TurnContext('Refactor module source.py.',profile='reviewer')
            runtime._prepare_turn(explicit)
            self.assertEqual(controller.records[-1]['before'],controller.records[-1]['after'])
            self.assertTrue(controller.records[-1]['child'])
        finally:controller.close()

    def test_planning_treatment_keeps_actual_accepted_strategic_bounds_and_explicit_plan_steps(self):
        from openkyrozen.agent.turn import TurnContext
        runtime=self.runtime()
        proposal={'title':'Approved source','summary':'One file only','bounds':{'workspace_paths':['source.py'],
            'capabilities':['read','write'],'effects':['read_only','filesystem'],'risk_ceiling':'normal'},
            'steps':[{'id':'source','title':'Source','description':'Write source.py','acceptance':[
                {'criterion':'Source preserved','verification':{'action':'read_file','arguments':{'path':'source.py'},
                                                           'expect':{'equals':'approved'}}}]}]}
        pending=runtime._interaction_controller.propose_plan(proposal)
        runtime._interaction_controller.accept_plan(runtime.tasks,plan_id=pending['plan_id'],version=1)
        before=copy.deepcopy(runtime._interaction_controller.state()['executing_plan'])
        tasks=runtime.tasks.working_plan_snapshot()
        controller=supplement().install(runtime,'planning-never')
        try:
            turn=TurnContext('Implement source.py, then verify the approved plan.',profile='auto')
            runtime._prepare_turn(turn)
            self.assertFalse(turn.planning_required)
            self.assertEqual(runtime._interaction_controller.state()['executing_plan'],before)
            self.assertEqual(runtime.tasks.working_plan_snapshot(),tasks)
        finally:controller.close()

    def test_symbolic_distinct_slots_and_main_pins_survive_the_actual_boundary(self):
        from openkyrozen.agent.turn import TurnContext
        runtime=self.runtime()
        runtime._provider_config.model_simple='symbolic-simple'
        runtime._provider_config.model_complex='symbolic-complex'
        controller=supplement().install(runtime,'planning-never')
        try:
            runtime._prepare_turn(TurnContext('Implement source.py, then verify.',profile='auto'))
            self.assertEqual(controller.records[-1]['before']['model'],'symbolic-complex')
            self.assertEqual(controller.records[-1]['after']['model'],'symbolic-complex')
            runtime._provider_config.model_main='explicit-main'
            runtime._prepare_turn(TurnContext('Hello',profile='coder'))
            self.assertEqual(controller.records[-1]['after']['model'],'explicit-main')
        finally:controller.close()

    def test_original_child_driver_receives_parent_variant_and_preserves_explicit_roles(self):
        from benchmarks import v3_actor_worker as actor
        from tests.test_v3_actor_worker import ActorDriverTests
        original=actor.run_actor
        observations=[]
        def run(payload,client,**kwargs):
            payload=copy.deepcopy(payload)
            payload['run']['assignment']['objective']='Refactor source.py, then verify the source evidence.'
            result=original(payload,client,**kwargs,routing_variant='planning-never')
            observations.extend(result['_benchmark_routing_observations'])
            return result
        with patch.object(actor,'run_actor',side_effect=run):
            ActorDriverTests('test_driver_keeps_real_author_and_review_scope_receipts_and_report_contract').test_driver_keeps_real_author_and_review_scope_receipts_and_report_contract()
        authors=[row for row in observations if row['interaction_mode']=='agent']
        reviews=[row for row in observations if row['interaction_mode']=='ask']
        self.assertTrue(authors and reviews)
        self.assertTrue(all(row['child'] for row in observations))
        self.assertTrue(any(row['before']['planning_required'] and not row['after']['planning_required'] for row in authors))
        self.assertTrue(all(row['before']==row['after'] for row in reviews))
        self.assertTrue(all(row['before']['profile']==row['after']['profile'] for row in observations))
        self.assertTrue(any(row['requests'] for row in observations))


class SupplementRunnerTests(unittest.TestCase):
    def prepare(self,hold='0'):
        from dataclasses import asdict
        from benchmarks.v3_matched import BudgetLedger
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING,DEEPSEEK_RELAY_CONFIG
        from benchmarks.v3_matched_live import _write_checkpoint
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        ledger=BudgetLedger(root/'core.db',limit_rmb='15',pricing=DEEPSEEK_PRICING)
        for identifier in ('smoke1','smoke2'):
            metadata={'reserved_rmb':'2.032768','arm':'smoke','fixture_id':'a'*40,'repeat':-1,
                      'model':'deepseek-flash','max_output_tokens':4096}
            ledger.reserve(identifier,input_token_bound=1000000,output_token_bound=4096,metadata=metadata)
            ledger.settle(identifier,{'input_tokens':10,'output_tokens':10,'cache_read_tokens':0,'cache_write_tokens':0})
        for index in range(int(Decimal(hold)/2)):
            identifier=f'past-{index}'
            ledger.reserve(identifier,input_token_bound=1000000,output_token_bound=4096,metadata={
                'reserved_rmb':'2.032768','arm':'v3','fixture_id':'past','repeat':0,
                'model':'deepseek-flash','max_output_tokens':4096})
            ledger.settle(identifier,{'input_tokens':1000000,'output_tokens':0,'cache_read_tokens':0,'cache_write_tokens':0})
        identity={'cohort_sha256':'core-cohort','apparatus_sha256':'apparatus','apparatus_files':{},'context_settings':{}}
        core={**identity,'protocol':'openkyrozen-v3-matched-live-v1','candidate_sha':'a'*40,
              'status':'complete','rows':[],'smoke_passed':True,'smoke_attempt_ids':['smoke1','smoke2'],
              'relay_config':asdict(DEEPSEEK_RELAY_CONFIG)}
        _write_checkpoint(root/'core.json',root/'core.db',core)
        return root,identity

    def call(self,root,identity,execute,credential,relay):
        from contextlib import ExitStack
        from benchmarks import v3_matched_live as live
        with ExitStack() as stack:
            for target,values in ((live,{'_source_snapshot':lambda source:'a'*40,
                'apparatus_identity':lambda:identity,'_require_dispatched_apparatus':lambda *args:Path('worker.py'),
                '_validate_rows_evidence':lambda *args:True,'_validate_worker_evidence':lambda *args:True,
                '_credential':credential,'_execute_cell':execute}),):
                stack.enter_context(patch.multiple(target,**values))
            stack.enter_context(patch('benchmarks.v3_matched.summarize',return_value={'complete':True,'accounting_complete':True}))
            stack.enter_context(patch('benchmarks.v3_protected_worker.verify_worker_protection'))
            stack.enter_context(patch('benchmarks.v3_responses_relay.ResponsesRelay',relay))
            return supplement().run(source=Path(__file__).resolve().parents[1],repo=Path(__file__).resolve().parents[1],
                core_output=root/'core.json',core_budget=root/'core.db',output=root/'supp.json',budget_path=root/'supp.db')

    def test_exhausted_continuation_stops_before_any_credential_or_relay_access_and_keeps_core_readonly(self):
        root,identity=self.prepare(hold='14')
        original=(root/'core.db').read_bytes(),(root/'core.json').read_bytes()
        def forbidden(*args,**kwargs):raise AssertionError('budget refusal accessed provider boundary')
        report=self.call(root,identity,forbidden,forbidden,forbidden)
        self.assertEqual(report['status'],'blocked_budget_reservation')
        self.assertFalse(report['summary']['benefit_passed'])
        self.assertEqual(((root/'core.db').read_bytes(),(root/'core.json').read_bytes()),original)
        from benchmarks.v3_deepseek import read_prior_exposure
        self.assertEqual(read_prior_exposure([root/'core.db']).total_cny,
                         read_prior_exposure([root/'core.db',root/'supp.db']).total_cny)

    def test_safety_failure_checkpoints_once_and_drops_raw_relay_body(self):
        root,identity=self.prepare()
        executed=[];relays=[]
        class Relay:
            def __init__(self,**values):
                self.rows=[];self.provider_error=None;self.budget_blocked=False;self.closed=False
                relays.append(self)
            def start(self):pass
            def close(self):self.closed=True
        def execute(**values):
            executed.append(values)
            values['relay'].rows.append({'attempt_id':'synthetic-only','arm':'routing-default',
                'fixture_id':'investigation-1','repeat':0,'response_body':'must-not-retain','status':'unknown'})
            return {'fixture_id':values['fixture']['fixture_id'],'repeat':values['repeat'],
                'arm':values['arm'],'variant':values['routing_variant'],'worker_result_complete':False,
                'safety_components':{'worker_complete':False},'routing_evidence_valid':False,'safety_passed':False}
        report=self.call(root,identity,execute,lambda:('unused','offline'),Relay)
        self.assertEqual(len(executed),1)
        self.assertEqual(report['status'],'blocked_safety_failure')
        self.assertTrue(relays[0].closed)
        self.assertNotIn('must-not-retain',(root/'supp.json').read_text())
        (root/'supp.json').unlink();(root/'supp.db').unlink()
        with self.assertRaisesRegex(ValueError,'missing.*continuation ledger'):
            self.call(root,identity,execute,lambda:('unused','offline'),Relay)
        self.assertEqual(len(executed),1)


_KERNEL_ROUTING = r'''
import contextlib,json,os,sys
from pathlib import Path
source,apparatus,workspace,url=sys.argv[1:5]
sys.path[:0]=[source,apparatus]
with contextlib.redirect_stdout(sys.stderr):
 from v3_process_owner import connect_actor_client
 from v3_actor_worker import install_delegated_adapter,install_mutation_adapter,install_read_adapter
 from v3_shell_launcher import install_worker_adapter
 from v3_routing_supplement import install
 from prompt_worker import _ProviderObservations
 from openkyrozen.app.bootstrap import build_application
 from openkyrozen.providers import ProviderConfig
 from openkyrozen.providers.openai import OpenAIResponsesProvider
 client=connect_actor_client()
 variant=client.rpc('routing_policy')['variant']
 install_worker_adapter(client=client);install_mutation_adapter(client);install_read_adapter(client)
 app=build_application(surface='cli');runtime=app.runtime
 controller=None
 try:
  runtime._set_workspace_root(workspace);runtime.set_interaction_mode('agent')
  config=ProviderConfig(provider='openai',api_key=os.environ['KYROZEN_BENCHMARK_API_KEY'],base_url=url,
      model_simple='deepseek-flash',model_complex='deepseek-flash',context_window_tokens=1000000)
  runtime._provider_config=config;runtime.llm_provider=OpenAIResponsesProvider(config)
  runtime._default_turn.provider=runtime.llm_provider
  runtime.DEEPSEEK_MODEL=runtime.DEEPSEEK_MODEL_SIMPLE=runtime.DEEPSEEK_MODEL_COMPLEX='deepseek-flash'
  runtime.MAX_STEPS_PER_TURN=1
  runtime.dispatch_learning_cycle=lambda **kwargs:None
  controller=install(runtime,variant);runtime._benchmark_routing_variant=variant
  observation=_ProviderObservations(runtime,runtime.llm_provider)
  install_delegated_adapter(runtime,client,source=source)
  runtime.chat(runtime.current_session,'Inspect catalog.py and report which function sorts products and its exact tie-break rule.',profile='auto')
  runtime.chat(runtime.current_session,'Refactor source.py, then verify the implementation.',profile='auto')
  coordinator=runtime.delegation()
  import threading
  run={'run_id':'routing-author','budget_id':'routing-author','profile':'researcher','name':'Author',
       'reviewer':'Reviewer','review_agent':{},'usage':{},'reviews':[],'status':'succeeded',
       'assignment':{'objective':'Refactor source.py, then verify its evidence.',
                     'context':'Inspect actual source','scope':['source.py']}}
  coordinator.runs[run['run_id']]=run;coordinator.cancelled[run['run_id']]=threading.Event()
  runtime._invoke_delegated(run,review=False,feedback=None,coordinator=coordinator)
  result={'variant':variant,'root':controller.records,'child':runtime._benchmark_child_routing_records}
 finally:
  app.close()
  if controller is not None:controller.close()
print(json.dumps(result))
'''


@unittest.skipUnless(hasattr(os,'geteuid') and os.geteuid()==0 and os.name=='posix',
                     'disposable privileged Unix native routing acceptance required')
class PrivilegedRoutingSupplementTests(unittest.TestCase):
    def test_parent_bound_root_and_child_routing_variants_use_original_native_runtime(self):
        import subprocess,sys,threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        from benchmarks.v3_shell_launcher import ShellPolicy
        from benchmarks.v3_process_owner import process_snapshot
        from benchmarks.v3_matched import BudgetLedger
        from benchmarks.v3_deepseek import DEEPSEEK_PRICING,DEEPSEEK_RELAY_CONFIG
        from benchmarks.v3_responses_relay import ResponsesRelay
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                text=json.dumps({'status':'completed','summary':'Offline source observation','findings':[],
                    'evidence':['source.py'],'artifacts':[],'checks':['offline'],
                    'uncertainties':[],'suggested_followups':[]})
                response={'id':'routing-local','object':'response','status':'completed','model':'deepseek-flash',
                    'output':[{'type':'message','id':'routing-message','role':'assistant','status':'completed',
                               'content':[{'type':'output_text','text':text,'annotations':[]}]}],
                    'usage':{'input_tokens':100,'output_tokens':20}}
                payload=(('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response})+'\n\n')
                         if body.get('stream') else json.dumps(response)).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)))
                self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json')
                self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        repository=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir='/var/tmp',prefix='routing-kernel-') as directory:
            base=Path(directory).resolve();base.chmod(0o711)
            for variant in supplement().VARIANTS:
                with self.subTest(variant=variant):
                    case=base/variant;case.mkdir(mode=0o711)
                    workspace=case/'workspace';workspace.mkdir()
                    (workspace/'source.py').write_text('original source')
                    guard=workspace/'test_guard.py';guard.write_text('original guard')
                    budget=BudgetLedger(case/'fake-relay.sqlite3',limit_rmb='15',pricing=DEEPSEEK_PRICING)
                    relay=ResponsesRelay(endpoint=f'http://127.0.0.1:{server.server_port}/v1',api_key='offline-only',
                                         budget=budget,config=DEEPSEEK_RELAY_CONFIG)
                    relay.label=('routing-'+variant,'kernel-routing',0);relay.start()
                    supervisor=None
                    try:
                        supervisor=ActorSupervisor([sys.executable,'-I','-c',_KERNEL_ROUTING,str(repository),
                            str(repository/'benchmarks'),str(workspace),relay.url,'--model','deepseek-flash'],
                            cwd=workspace,env={'PATH':os.environ.get('PATH',os.defpath),
                                'KYROZEN_CLI_CAPABILITIES':'read,write,shell,network',
                                'KYROZEN_DISABLE_VECTOR_INDEX':'1','KYROZEN_DECISION_ASSIST_BACKEND':'off'},
                            protected_files=[guard],policy=ShellPolicy(workspace,{'read','write','shell','network'}),
                            source=repository,provider_config=relay.issue_actor_config,revoke_actor=relay.revoke_actor,
                            routing_variant=variant)
                        with supervisor:
                            process=supervisor.popen(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                            output,error=process.communicate(timeout=90)
                            self.assertEqual(process.returncode,0,error.decode(errors='replace'))
                            result=json.loads(output)
                            self.assertEqual(result['variant'],variant)
                            self.assertTrue(result['root'] and result['child'])
                            for row in result['root']+result['child']:
                                self.assertEqual(row['variant'],variant)
                                self.assertEqual(row['after'],asdict(supplement().intervene(
                                    __import__('openkyrozen.routing.router',fromlist=['RoutingDecision']).RoutingDecision(**row['before']),
                                    variant,profile_override=row['profile_override'],interaction_mode=row['interaction_mode'])))
                            self.assertTrue(any(row['requests'] for row in result['root']))
                            if variant=='planning-never':
                                self.assertTrue(any(row['before']['planning_required'] and not row['after']['planning_required']
                                                    and row['requests'] for row in result['child']))
                            self.assertTrue(all(row['before']['profile']==row['after']['profile'] for row in result['child']))
                    finally:relay.close()
                    owners={owner.uid for owner in supervisor.owners}
                    self.assertFalse(any(uid in owners and state!='Z' for uid,state in process_snapshot().values()))
                    self.assertEqual(guard.read_text(),'original guard')
            self.assertTrue(requests)
            self.assertTrue(all(row['model']=='deepseek-flash' and row['max_output_tokens']==4096 for row in requests))
