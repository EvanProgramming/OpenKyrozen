import unittest
from openkyrozen.tools.manifest import ToolRegistry
from openkyrozen.tools import ToolAdapters

class RegistryRegressions(unittest.TestCase):
    def test_every_adapter_has_complete_spec(self):
        adapters = ToolAdapters()
        self.assertEqual(len(adapters.AVAILABLE_TOOLS),40)
        for name, executor in adapters.AVAILABLE_TOOLS.items():
            spec = adapters.tool_registry.get_spec(name)
            self.assertIs(spec.executor,executor)
            self.assertTrue(spec.description)
            self.assertEqual(spec.input_schema['type'],'object')
            self.assertTrue(spec.capability)
            self.assertTrue(spec.side_effects)
            self.assertIsInstance(spec.parallel_safe,bool)

    def test_compatibility_registration_has_restrictive_metadata(self):
        adapters = ToolAdapters()
        adapters.AVAILABLE_TOOLS['new_helper'] = lambda args: args
        spec = adapters.tool_registry.get_spec('new_helper')
        self.assertEqual(spec.capability,'dynamic')
        self.assertFalse(spec.parallel_safe)
        self.assertEqual(spec.input_schema['properties'],{'args':{'type':'string'}})

    def test_mcp_provider_schemas_cannot_drift_or_mutate_catalog(self):
        adapters = ToolAdapters()
        registry = adapters.tool_registry
        mcp = {s['name']:s for s in registry.mcp_descriptors()}
        provider = {s['name']:s for s in registry.provider_schemas()}
        self.assertEqual(mcp['write_file']['inputSchema'],provider['write_file']['parameters'])
        mcp['write_file']['inputSchema']['properties']['path']['type']='number'
        self.assertEqual(registry.get_spec('write_file').input_schema['properties']['path']['type'],'string')
        self.assertNotIn('bash',provider)

class RegistryContractTests(unittest.TestCase):
    def make(self,name='custom',**changes):
        from openkyrozen.tools import ToolSpec
        values=dict(description='Custom helper.',input_schema={'type':'object','properties':{'args':{'type':'string'}},'additionalProperties':False},
                    capability='read',side_effects='read',parallel_safe=False,aliases=('helper_alias',),source='test',version='2.0.0')
        values.update(changes)
        return ToolSpec(name,lambda args:args,**values)

    def test_collision_and_invalid_registration_are_atomic(self):
        registry=ToolRegistry()
        spec=self.make()
        registry.register_spec(spec)
        for conflicting in (spec,self.make('helper_alias',aliases=()),self.make('other')):
            with self.assertRaises(ValueError):
                registry.register_spec(conflicting)
            self.assertEqual(dict(registry.specs),{'custom':spec})
        for change in ({'capability':'admin'},{'parallel_safe':True,'side_effects':'stateful'},
                       {'input_schema':{'type':'object','properties':{},'required':['missing'],'additionalProperties':False}},
                       {'input_schema':{'type':'object','properties':{},'additionalProperties':False,'default':float('nan')}}):
            with self.assertRaises(ValueError):
                self.make(**change)

    def test_snapshot_restore_preserves_complete_metadata(self):
        registry=ToolRegistry()
        spec=self.make(visibility='hidden',risk='high')
        registry.register_spec(spec)
        snapshot=registry.snapshot()
        registry.tools['custom']=lambda args:'changed'
        self.assertEqual(registry.get_spec('custom').manifest,spec.manifest)
        del registry.tools['custom']
        registry.register_spec(snapshot['custom'])
        self.assertIs(registry.get_spec('custom'),spec)
        self.assertEqual(registry.mcp_descriptors(),[])

    def test_specs_are_immutable_including_schema_and_aliases(self):
        from dataclasses import FrozenInstanceError
        schema={'type':'object','properties':{'args':{'type':'string'}},'additionalProperties':False}
        spec=self.make(input_schema=schema)
        schema['properties']['args']['type']='number'
        self.assertEqual(spec.input_schema['properties']['args']['type'],'string')
        with self.assertRaises(FrozenInstanceError):
            spec.capability='dynamic'

    def test_schema_conversion_examples_and_legacy_aliases(self):
        registry=ToolAdapters().tool_registry
        cases={'read_file':({'file_path':'a'},'a'),'write_file':({'path':'a','content':'x|y'},'a|x|y'),
               'find_files':({'pattern':'*.py'},'*.py'),'run_cmd':({'cmd':'echo hi'},'echo hi'),
               'browser_type':({'sessionId':'s','selector':'x','text':'y'},'s|x|y')}
        for name,(arguments,expected) in cases.items():
            self.assertEqual(registry.get_spec(name).legacy_arguments(arguments),expected)
        with self.assertRaises(ValueError):
            registry.get_spec('read_file').legacy_arguments({'path':1})

class RuntimeCatalogTests(unittest.TestCase):
    def test_all48_bound_tools_and_workspace_isolation(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application,build_memory
        with tempfile.TemporaryDirectory() as home,patch.dict('os.environ',{'KYROZEN_DISABLE_VECTOR_INDEX':'1'}):
            app=build_application(memory=build_memory(Path(home)/'state.sqlite3'))
            try:
                runtime=app.runtime
                self.assertEqual(len(runtime.AVAILABLE_TOOLS),48)
                self.assertEqual(set(runtime.AVAILABLE_TOOLS),set(runtime.tool_registry.specs))
                first=Path(home)/'first';second=Path(home)/'second';first.mkdir();second.mkdir()
                runtime._set_workspace_root(first)
                original=runtime.tool_registry
                runtime.AVAILABLE_TOOLS['private_helper']=lambda args:'first'
                runtime._set_workspace_root(second)
                self.assertNotIn('private_helper',runtime.AVAILABLE_TOOLS)
                self.assertIsNot(runtime.tool_registry,original)
                runtime._set_workspace_root(first)
                self.assertEqual(runtime.AVAILABLE_TOOLS['private_helper'](''),'first')
                runtime.execution_context.child_run_id='child'
                self.assertNotIn('spawn_agents',runtime.AVAILABLE_TOOLS)
            finally:
                app.close()

class SchemaValidationRegressions(unittest.TestCase):
    def test_schema_rejects_non_json_keys_and_bad_nested_keywords(self):
        helper=RegistryContractTests()
        for schema in ({'type':'object','properties':{1:{'type':'string'}},'additionalProperties':False},
                       {'type':'object','properties':{'args':{'type':'string','enum':[]}},'additionalProperties':False},
                       {'type':'object','properties':{'args':{'type':'array','items':{'type':'invalid'}}},'additionalProperties':False}):
            with self.subTest(schema=schema),self.assertRaises(ValueError):
                helper.make(input_schema=schema)

    def test_alias_collections_and_legacy_fields_have_explicit_types(self):
        helper=RegistryContractTests()
        for change in ({'aliases':'xyz'},{'legacy_fields':'args'},{'legacy_argument_aliases':(('args','args'),)},
                       {'legacy_argument_aliases':(('args','x'),('args','x'))}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                helper.make(legacy_fields=('args',),**change) if 'legacy_fields' not in change else helper.make(**change)

class PolicyMetadataRegressions(unittest.TestCase):
    def test_custom_risk_metadata_reaches_permission_gate(self):
        from openkyrozen.security.permission_gate import risk_category
        registry=ToolRegistry()
        registry.register_spec(RegistryContractTests().make(risk='high'))
        self.assertEqual(risk_category('custom','',registry.tools),'external_or_irreversible_change')

class ViewAuthorizationRegressions(unittest.TestCase):
    def test_filtered_mapping_cannot_overwrite_hidden_executor(self):
        from openkyrozen.tools.manifest import ToolMapping
        registry=ToolRegistry({'spawn_agents':lambda args:'original'})
        child=ToolMapping(registry,excluded=('spawn_agents',))
        with self.assertRaises(PermissionError):
            child['spawn_agents']=lambda args:'overwritten'
        self.assertEqual(registry.tools['spawn_agents'](''),'original')

class ReviewRegressions(unittest.TestCase):
    def test_invalid_constraints_and_unsupported_keywords_rejected(self):
        helper=RegistryContractTests()
        for prop in ({'type':'string','minLength':-1},{'type':'string','pattern':42},
                     {'type':'string','unexpected':'value'},{'type':'string','minLength':True},{'type':'string','examples':42}):
            with self.subTest(prop=prop),self.assertRaises(ValueError):
                helper.make(input_schema={'type':'object','properties':{'args':prop},'additionalProperties':False})

    def test_alias_collision_is_rejected_before_dynamic_approval(self):
        from unittest.mock import Mock,patch
        from types import SimpleNamespace as NS
        from openkyrozen.tools.dynamic import _register_tool
        from openkyrozen.security.capabilities import issue_capability_token
        registry=ToolAdapters().tool_registry
        rejected=Mock(return_value=False)
        approved=Mock(return_value=True)
        runtime=NS(ALLOW_DYNAMIC_TOOLS=True,tool_registry=registry,AVAILABLE_TOOLS=registry.tools,
                   _get_workspace_root=lambda:'.',_execution_capability_token=issue_capability_token('test',{'dynamic'}),
                   _reject_dynamic_tool=rejected,_confirm_tool_action=approved)
        with patch('openkyrozen.tools.dynamic.effective_capabilities',return_value={'dynamic'}),patch('openkyrozen.tools.dynamic.load_agent_config'):
            self.assertFalse(_register_tool(runtime,'bash','def bash(args):\n    return args'))
        rejected.assert_called_once()
        approved.assert_not_called()

class LiveInventoryRegressions(unittest.TestCase):
    def test_registered_names_and_aliases_immediately_parse(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application,build_memory
        with tempfile.TemporaryDirectory() as home,patch.dict('os.environ',{'KYROZEN_DISABLE_VECTOR_INDEX':'1'}):
            app=build_application(memory=build_memory(Path(home)/'state.sqlite3'))
            try:
                runtime=app.runtime
                spec=RegistryContractTests().make()
                runtime.tool_registry.register_spec(spec)
                for name in ('custom','helper_alias'):
                    calls=runtime._collect_tool_calls(name+': hello')
                    self.assertEqual(calls,[{'action':'custom','args':'hello'}])
                filter=runtime.DeepSeekDSMLFilter()
                self.assertNotIn('helper_alias:',filter.feed('helper_alias: hello',final=True))
            finally:
                app.close()

class GitHubReviewRegressions(unittest.TestCase):
    def test_combinator_properties_register_but_nonstring_legacy_fields_do_not(self):
        helper=RegistryContractTests()
        schema={'type':'object','properties':{'args':{'oneOf':[{'type':'string'},{'type':'number'}]}},'additionalProperties':False}
        self.assertEqual(helper.make(input_schema=schema).input_schema,schema)
        schema['properties']['args']['type']=None
        with self.assertRaises(ValueError):
            helper.make(input_schema=schema)
        for kind in ('integer','number','boolean','object','array'):
            schema={'type':'object','properties':{'args':{'type':kind}},'additionalProperties':False}
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                helper.make(input_schema=schema,legacy_fields=('args',))

    def test_case_insensitive_identity_collisions_rejected(self):
        registry=ToolRegistry()
        registry.register_spec(RegistryContractTests().make('MyTool',aliases=('MyAlias',)))
        for name in ('mytool','MYALIAS'):
            with self.assertRaises(ValueError):
                registry.register_spec(RegistryContractTests().make(name,aliases=()))

    def test_runtime_removal_and_case_preserving_parser(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application,build_memory
        with tempfile.TemporaryDirectory() as home,patch.dict('os.environ',{'KYROZEN_DISABLE_VECTOR_INDEX':'1'}):
            app=build_application(memory=build_memory(Path(home)/'state.sqlite3'))
            try:
                runtime=app.runtime
                runtime.tool_registry.register_spec(RegistryContractTests().make('MyTool',aliases=('MyAlias',)))
                for spelling in ('MyTool','mytool','MYALIAS'):
                    for text in (spelling+': hello', '<action>\n'+spelling+'\nhello\n</action>',
                                 '<｜DSML｜invoke name="'+spelling+'"><｜DSML｜parameter name="args">hello</｜DSML｜parameter></｜DSML｜invoke>'):
                        with self.subTest(text=text):
                            self.assertEqual(runtime._collect_tool_calls(text),[{'action':'MyTool','args':'hello'}])
                for name in ('spawn_agents','discover_tools'):
                    del runtime.AVAILABLE_TOOLS[name]
                    self.assertNotIn(name,runtime.AVAILABLE_TOOLS)
                    self.assertNotIn(name,runtime.tool_registry.specs)
                runtime.AVAILABLE_TOOLS=dict(runtime.AVAILABLE_TOOLS)
                self.assertNotIn('spawn_agents',runtime.AVAILABLE_TOOLS)
            finally:
                app.close()
