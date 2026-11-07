"""Internal benchmark adapter; production delegation still owns its behavior."""
from __future__ import annotations

import json

_MAX_PAYLOAD = 1_000_000
_MUTATIONS = frozenset({'write_file', 'edit_file', 'apply_patch'})
_READS = frozenset({'read_file', 'search_files', 'list_dir', 'list_tree', 'find_files'})


def _json_copy(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > _MAX_PAYLOAD:
        raise ValueError('benchmark actor payload exceeds its bound')
    return json.loads(encoded)


def _require_selected_source(source):
    import openkyrozen
    from pathlib import Path
    root = Path(source).resolve(strict=True)
    if Path(openkyrozen.__file__).resolve() != root / 'openkyrozen' / '__init__.py':
        raise PermissionError('loaded product does not match the selected source')


def _install_file_adapter(client, operation, actions):
    from functools import wraps
    from openkyrozen.tools import ToolAdapters
    from openkyrozen.tools import models

    def mutation(action):
        @wraps(getattr(ToolAdapters, action))
        def invoke(self, arguments):
            result = client.rpc(operation, action=action, arguments=_json_copy(arguments))
            if hasattr(models, 'ToolResult'):
                error = result.get('error')
                return models.ToolResult(data=result.get('data'), text=result.get('text', ''),
                    error=models.ToolError(**error) if error else None, metadata=result.get('metadata', {}))
            if not isinstance(result, str):
                raise TypeError('legacy file helper must return the original string result')
            return result
        return models.legacy_text_api(invoke) if hasattr(models, 'legacy_text_api') else invoke

    for action in actions:
        if hasattr(ToolAdapters, action):
            setattr(ToolAdapters, action, mutation(action))


def install_mutation_adapter(client):
    """Move only the original builtin filesystem effect across the boundary."""
    _install_file_adapter(client, 'file_mutation', _MUTATIONS)


def install_read_adapter(client):
    """Read live shared files even when the actor has an older readonly view."""
    _install_file_adapter(client, 'file_read', _READS)


def install_delegated_adapter(runtime, client, *, source):
    """Replace transport before the real Delegation captures its invocation port."""
    import inspect
    import time
    import uuid
    from dataclasses import asdict
    from pathlib import Path
    from types import MethodType
    from contextlib import ExitStack

    if runtime.subagent_manager.coordinator is not None:
        raise RuntimeError('install the actor adapter before creating the coordinator')
    original = runtime._invoke_delegated
    supports_repair = 'report_repair' in inspect.signature(original).parameters
    source = str(Path(source).resolve(strict=True))
    runtime._benchmark_child_context_records = []

    def invoke(self, run, *, review, feedback, coordinator, report_repair=False):
        if report_repair and not supports_repair:
            raise RuntimeError('selected snapshot does not support delegated report repair')
        parent = self.current_session
        budgets = getattr(coordinator, 'budgets', {})
        regressions = parent.memory.store.list_events('learning.regression_case_created', limit=1,
            user_id=parent.memory.user_id, workspace_id=parent.memory.workspace_id)
        if self.skill_registry.list() or regressions:
            raise RuntimeError('benchmark actor does not support existing skill or learning regression state')
        if any(getattr(parent.workspace, name, None) is not None
               for name in ('launch_context', 'graph', 'github')):
            raise RuntimeError('benchmark actor cannot copy live graph or launch-context services')
        payload = _json_copy({
            'schema_version': 1, 'source': source, 'workspace': str(coordinator.root),
            'run': run, 'review': review, 'feedback': feedback, 'report_repair': report_repair,
            'supports_report_repair': supports_repair,
            'messages': ({} if review else {key: value for key, value in coordinator.messages.items()
                         if key in {run['run_id'], run.get('budget_id', run['run_id'])}}),
            'parent': {'session_id': parent.session_id, 'user_id': parent.memory.user_id,
                       'workspace_id': parent.memory.workspace_id,
                       'task_workspace_id': parent.tasks.workspace_id,
                       'capabilities': sorted(self._execution_capability_token.capabilities),
                       'interaction_mode': self._active_interaction_mode.get(),
                       'model': self.DEEPSEEK_MODEL,
                       'has_budget': budgets.get(run['run_id']) is not None,
                       'profiles': [asdict(profile) for profile in self.subagent_manager.profiles.values()]}})
        identifier = client.rpc('delegated_launch', payload=payload)['id']
        run_id = run['run_id']
        budget_id = run.get('budget_id', run_id)
        access_handles = {}

        def cancelled():
            event = coordinator.cancelled.get(run_id)
            parent_cancelled = getattr(self.execution_context, 'cancelled', None)
            return bool((event is not None and event.is_set())
                        or (parent_cancelled is not None and parent_cancelled.is_set()))

        def owned_session(session_id):
            valid = (isinstance(session_id, str) and
                     (any(session_id.startswith(key + '_review_') for key in {budget_id, run_id})
                      if review else session_id in {budget_id, run_id}))
            if not valid:
                raise PermissionError('session does not belong to this actor invocation')
            return session_id

        def scoped_memory(session_id):
            return parent.memory.scoped(session_id=owned_session(session_id))

        def serve(request, stack):
            values = request['values']
            operation = request['op']
            if operation == 'cancelled':
                return cancelled()
            if operation == 'publish':
                update = values['run']
                if set(update) - {'provider_model', 'review_agent'}:
                    raise PermissionError('child cannot change its assignment or parent policy')
                with coordinator.lock:
                    for name, value in update.items():
                        if name == 'review_agent':
                            if set(value) - {'status', 'provider_model'}:
                                raise PermissionError('invalid reviewer state update')
                            run['review_agent'].update(value)
                        else:
                            run[name] = value
                    coordinator._publish(run)
                return None
            if operation == 'receipt':
                return coordinator.record_receipt(run_id, owned_session(values['child_id']), values['receipt'])
            if operation == 'message':
                return coordinator.add_message(owned_session(values['child_id']), values['message'])
            if operation == 'usage':
                with coordinator.lock:
                    for child_id in values['usage']:
                        owned_session(child_id)
                    run.setdefault('usage', {}).update(values['usage'])
                return None
            if operation in {'budget_can_continue', 'budget_record_cost'}:
                budget = budgets.get(run_id)
                if budget is None:
                    raise PermissionError('this invocation has no delegated budget')
                if operation == 'budget_can_continue':
                    return budget.can_continue(budget_id, cost_picos=values['cost_picos'])
                return budget.record_cost(budget_id, values['cost_picos'])
            if operation == 'access_acquire':
                context = coordinator.access.acquire(values['path'], cancelled, run_id,
                    reading=values['reading'], **({'recursive': values['recursive']}
                    if 'recursive' in inspect.signature(coordinator.access.acquire).parameters else {}))
                context.__enter__()
                token = uuid.uuid4().hex
                access_handles[token] = context
                stack.callback(lambda: access_handles.pop(token).__exit__(None, None, None)
                               if token in access_handles else None)
                return token
            if operation == 'access_release':
                context = access_handles.pop(values['token'])
                context.__exit__(None, None, None)
                return None
            if operation == 'event':
                return self._emit_stream_event(values['event'])
            if operation == 'parent_interaction_state':
                return parent.interaction.state()
            if operation == 'parent_task_state':
                return {'tasks': parent.tasks.tasks,
                        'working_plan': asdict(parent.tasks.working_plan) if parent.tasks.working_plan else None}
            if operation == 'parent_active_task':
                return parent.tasks.active_working_task(values['task_id'])
            if operation == 'parent_mutation_matches_current_task':
                return parent.tasks.mutation_matches_current_task(values['action'], values['arguments'])
            if operation in {'memory_recall_records', 'memory_recall', 'memory_recent'}:
                memory = scoped_memory(values['session_id'])
                options = dict(values.get('options', {}))
                if operation == 'memory_recall_records':
                    if 'authorized_speakers' in options:
                        options['authorized_speakers'] = set(options['authorized_speakers'])
                    return memory.recall_records(values['query'], **options)
                if operation == 'memory_recall':
                    return memory.recall(values['query'], **options)
                return memory.get_recent(**options)
            if operation == 'usage_attempts':
                recorder = parent.memory.store.record_usage_attempt
                keys = set(inspect.signature(recorder).parameters)
                for attempt in values['attempts']:
                    owned_session(attempt['session_id'])
                    if (attempt['user_id'] != parent.memory.user_id
                            or attempt['workspace_id'] != parent.memory.workspace_id):
                        raise PermissionError('usage belongs to another actor scope')
                    item = {key: value for key, value in attempt.items() if key in keys}
                    item['attempt_id'] = attempt['id']
                    recorder(**item)
                return None
            raise PermissionError('unsupported delegated coordinator operation')

        try:
            with ExitStack() as stack:
                replies = []
                while True:
                    response = client.rpc('delegated_poll', id=identifier,
                                          replies=replies, cancelled=cancelled())
                    replies = []
                    for request in response.get('requests', []):
                        try:
                            value = _json_copy(serve(request, stack))
                            replies.append({'id': request['id'], 'ok': True, 'value': value})
                        except Exception as exc:
                            replies.append({'id': request['id'], 'ok': False,
                                            'error': f'{type(exc).__name__}: {str(exc)[:1000]}'})
                    if response['state'] == 'complete':
                        if replies:
                            raise RuntimeError('actor completed with unanswered coordinator operations')
                        result = _json_copy(response['result'])
                        records = result.pop('_benchmark_request_context_records', [])
                        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                            raise ValueError('invalid child context observations')
                        self._benchmark_child_context_records.extend(records)
                        return result
                    if response['state'] == 'failed':
                        raise RuntimeError(response.get('error', 'benchmark actor failed'))
                    if response['state'] != 'running':
                        raise RuntimeError('invalid delegated actor state')
                    time.sleep(.01)
        finally:
            client.rpc('delegated_stop', id=identifier)

    runtime._invoke_delegated = MethodType(invoke, runtime)


class _CoordinatorPort:
    def __init__(self, payload, client):
        import threading
        from pathlib import Path
        self.client = client
        self.root = Path(payload['workspace']).resolve(strict=True)
        self.run = payload['run']
        self.review = payload['review']
        self.run_id = self.run['run_id']
        self.budget_id = self.run.get('budget_id', self.run_id)
        self.runs = {self.run_id: self.run}
        self.messages = payload['messages']
        self.lock = threading.RLock()
        self.cancelled = {self.run_id: _CancellationPort(self)}
        self.access = _AccessPort(self)
        self.budgets = {self.run_id: _BudgetPort(self)} if payload['parent']['has_budget'] else {}

    def call(self, operation, **values):
        return self.client.rpc('delegated_call', operation=operation, values=_json_copy(values))

    def _publish(self, run):
        values = ({'review_agent': {key: run['review_agent'][key]
                                   for key in ('status', 'provider_model') if key in run['review_agent']}}
                  if self.review else {'provider_model': run['provider_model']})
        self.call('publish', run=values)

    def check_cancelled(self, run_id):
        if run_id != self.run_id or self.cancelled[self.run_id].is_set():
            raise RuntimeError('Sub-agent cancelled')

    def record_receipt(self, run_id, child_id, receipt):
        if run_id != self.run_id:
            raise PermissionError('receipt belongs to another actor')
        return self.call('receipt', child_id=child_id, receipt=receipt)

    def add_message(self, child_id, message):
        self.call('message', child_id=child_id, message=message)
        self.messages.setdefault(child_id, []).append(message)


class _CancellationPort:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def is_set(self):
        return self.coordinator.call('cancelled')


class _BudgetPort:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def can_continue(self, run_id, *, cost_picos):
        if run_id != self.coordinator.budget_id:
            raise PermissionError('budget belongs to another actor')
        return self.coordinator.call('budget_can_continue', cost_picos=cost_picos)

    def record_cost(self, run_id, cost_picos):
        if run_id != self.coordinator.budget_id:
            raise PermissionError('budget belongs to another actor')
        return self.coordinator.call('budget_record_cost', cost_picos=cost_picos)


class _AccessPort:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def acquire(self, path, cancelled, owner, *, reading=False, recursive=False):
        from contextlib import contextmanager

        @contextmanager
        def claim():
            token = self.coordinator.call('access_acquire', path=str(path) if path is not None else None,
                                          reading=reading, recursive=recursive)
            try:
                yield
            finally:
                self.coordinator.call('access_release', token=token)
        return claim()


class _ParentTasksPort:
    def __init__(self, coordinator, identity):
        self.coordinator = coordinator
        self.user_id = identity['user_id']
        self.workspace_id = identity['task_workspace_id']
        self.session_id = identity['session_id']

    @property
    def tasks(self):
        return self.coordinator.call('parent_task_state')['tasks']

    @property
    def working_plan(self):
        from types import SimpleNamespace
        plan = self.coordinator.call('parent_task_state')['working_plan']
        return SimpleNamespace(**plan) if plan else None

    def active_working_task(self, task_id):
        return self.coordinator.call('parent_active_task', task_id=task_id)

    def mutation_matches_current_task(self, action, arguments):
        return self.coordinator.call('parent_mutation_matches_current_task', action=action, arguments=arguments)

    @staticmethod
    def _dependencies_succeeded(task, tasks):
        from openkyrozen.tasks.engine import TaskManager
        return TaskManager._dependencies_succeeded(task, tasks)


class _ParentInteractionPort:
    def __init__(self, coordinator, identity):
        self.coordinator = coordinator
        self.user_id = identity['user_id']
        self.workspace_id = identity['task_workspace_id']
        self.session_id = identity['session_id']

    def state(self):
        return self.coordinator.call('parent_interaction_state')


def run_actor(payload, client, *, source, provider_config):
    """Run the selected snapshot's original child invocation in a fresh process."""
    import inspect
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from openkyrozen.app.bootstrap import build_application
    from openkyrozen.agent.subagents import AgentProfile
    from openkyrozen.security.capabilities import issue_capability_token

    _require_selected_source(source)
    payload = _json_copy(payload)
    if (payload['schema_version'] != 1 or Path(payload['source']).resolve() != Path(source).resolve()
            or type(payload['review']) is not bool or type(payload['report_repair']) is not bool):
        raise PermissionError('invalid benchmark actor invocation')
    if os.environ.get('KYROZEN_BENCHMARK_BROKER_FD') is not None:
        raise PermissionError('actor must not inherit another worker broker descriptor')
    if hasattr(client, 'start'):
        from v3_shell_launcher import install_worker_adapter
        install_worker_adapter(client=client)
    install_mutation_adapter(client)
    install_read_adapter(client)
    application = build_application(surface='cli', user_id=payload['parent']['user_id'])
    runtime = application.runtime
    coordinator = _CoordinatorPort(payload, client)
    original = runtime._invoke_delegated
    original_create_session = runtime._create_session
    try:
        from prompt_worker import _ProviderObservations
    except ModuleNotFoundError:
        from benchmarks.prompt_worker import _ProviderObservations
    original_provider = runtime.get_provider
    observations = []

    def get_provider(config):
        provider = original_provider(config)
        observation = _ProviderObservations(runtime, provider)
        observations.append(observation)
        return provider

    runtime.get_provider = get_provider

    def create_session(*args, **kwargs):
        session = original_create_session(*args, **kwargs)
        session_id = session.memory.session_id
        def memory_call(operation, query=None, **options):
            if 'authorized_speakers' in options:
                options['authorized_speakers'] = sorted(options['authorized_speakers'])
            return coordinator.call(operation, session_id=session_id, query=query, options=options)
        session.memory.recall_records = lambda query, **options: memory_call('memory_recall_records', query, **options)
        session.memory.recall = lambda query, **options: memory_call('memory_recall', query, **options)
        session.memory.get_recent = lambda n=10, **options: memory_call('memory_recent', n=n, **options)
        return session

    try:
        runtime._set_workspace_root(payload['workspace'])
        identity = payload['parent']
        parent = runtime.open_session(identity['session_id'], user_id=identity['user_id'])
        parent.memory.workspace_id = identity['workspace_id']
        parent.tasks = _ParentTasksPort(coordinator, identity)
        parent.interaction = _ParentInteractionPort(coordinator, identity)
        runtime._create_session = create_session
        runtime.dispatch_learning_cycle = lambda **kwargs: None
        runtime._plugin_runtime_for_surface = lambda *args: SimpleNamespace(
            turn_start=lambda **kwargs: None, turn_end=lambda **kwargs: None)
        chat = runtime.chat
        runtime.chat = lambda session, message, **kwargs: chat(session, message,
            on_event=lambda event: coordinator.call('event', event=event), **kwargs)
        with runtime.use_session(parent):
            runtime._provider_config = provider_config
            runtime.DEEPSEEK_MODEL_SIMPLE = provider_config.model_simple
            runtime.DEEPSEEK_MODEL_COMPLEX = provider_config.model_complex
            runtime.DEEPSEEK_MODEL = identity['model']
            runtime._execution_capability_token = issue_capability_token('benchmark-parent-view',
                                                                         identity['capabilities'])
            if identity['profiles']:
                runtime.subagent_manager.profiles = {profile['name']: AgentProfile(**profile)
                                                    for profile in identity['profiles']}
            mode_token = runtime._active_interaction_mode.set(identity['interaction_mode'])
            runtime.execution_context.cancelled = coordinator.cancelled[coordinator.run_id]
            try:
                kwargs = {'review': payload['review'], 'feedback': payload['feedback'], 'coordinator': coordinator}
                supports_repair = 'report_repair' in inspect.signature(original).parameters
                if payload['report_repair'] and not supports_repair:
                    raise RuntimeError('selected snapshot does not support delegated report repair')
                if supports_repair:
                    kwargs['report_repair'] = payload['report_repair']
                result = original(coordinator.run, **kwargs)
                records = [dict(record, actor_run_id=coordinator.run_id, review=payload['review'])
                           for observation in observations for record in observation.records]
                return dict(result, _benchmark_request_context_records=records)
            finally:
                runtime._active_interaction_mode.reset(mode_token)
                usage = coordinator.run.get('usage', {})
                prefixes = (coordinator.budget_id + '_review_', coordinator.run_id + '_review_')
                coordinator.call('usage', usage={key: value for key, value in usage.items()
                    if (key.startswith(prefixes) if payload['review']
                        else key in {coordinator.run_id, coordinator.budget_id})})
                attempts = application.store.list_usage_attempts(user_id=identity['user_id'], limit=10000)
                coordinator.call('usage_attempts', attempts=attempts)
    finally:
        application.close()


def _run_file_tool(payload, *, source, actions):
    _require_selected_source(source)
    from openkyrozen.tools import ToolAdapters, models
    payload = _json_copy(payload)
    if set(payload) != {'workspace', 'action', 'arguments'} or payload['action'] not in actions:
        raise PermissionError('invalid trusted file tool request')
    adapter = ToolAdapters(payload['workspace'])
    method = getattr(adapter, payload['action'])
    if hasattr(models, 'invoke_tool_result'):
        result = models.invoke_tool_result(method, payload['arguments'])
        error = result.error
        return {'data': result.data, 'text': result.text,
                'error': ({'code': error.code, 'message': error.message, 'retriable': error.retriable,
                           'metadata': error.metadata} if error else None), 'metadata': result.metadata}
    result = method(payload['arguments'])
    if not isinstance(result, str):
        raise TypeError('legacy file tool returned an unexpected result')
    return result


def run_file_mutation(payload, *, source):
    """Trusted helper: selected original mutator only; no application startup."""
    return _run_file_tool(payload, source=source, actions=_MUTATIONS)


def run_file_read(payload, *, source):
    """Trusted helper: selected original readonly tool; no application startup."""
    return _run_file_tool(payload, source=source, actions=_READS)


def main():
    import argparse
    import sys
    from contextlib import redirect_stdout
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    files = parser.add_mutually_exclusive_group()
    files.add_argument('--file-mutation', action='store_true')
    files.add_argument('--file-read', action='store_true')
    arguments = parser.parse_args()
    source = Path(arguments.source).resolve(strict=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(source))
    with redirect_stdout(sys.stderr):
        if arguments.file_mutation or arguments.file_read:
            encoded = sys.stdin.buffer.read(_MAX_PAYLOAD + 1)
            if len(encoded) > _MAX_PAYLOAD:
                raise ValueError('file tool request exceeds benchmark limit')
            operation = run_file_mutation if arguments.file_mutation else run_file_read
            result = operation(json.loads(encoded), source=source)
        else:
            from v3_process_owner import connect_actor_client
            from openkyrozen.providers.config import ProviderConfig
            client = connect_actor_client()
            bootstrap = client.rpc('actor_bootstrap')
            result = run_actor(bootstrap['payload'], client, source=source,
                               provider_config=ProviderConfig(**bootstrap['provider_config']))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
