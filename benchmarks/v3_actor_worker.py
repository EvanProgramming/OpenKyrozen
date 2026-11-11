"""Internal benchmark adapter; production delegation still owns its behavior."""
from __future__ import annotations

import json

_MAX_PAYLOAD = 1_000_000
_MUTATIONS = frozenset({'write_file', 'edit_file', 'apply_patch'})
_READS = frozenset({'read_file', 'search_files', 'list_dir', 'list_tree', 'find_files'})
_HISTORY_CHUNK = 65_536


def benchmark_provider_config(values, **extra):
    """The original baseline has implicit Auto routing and no model_main field."""
    from dataclasses import fields
    from openkyrozen.providers.config import ProviderConfig
    if set(values) & set(extra):
        raise TypeError('duplicate benchmark provider configuration option')
    options = dict(values, **extra)
    if 'model_main' not in {field.name for field in fields(ProviderConfig)} and 'model_main' in options:
        if options.pop('model_main') != 'auto':
            raise ValueError('original baseline supports only implicit Auto routing')
    return ProviderConfig(**options)


def benchmark_root_provider_config(protocol, model, url, api_key, context_window_tokens):
    import os
    provider = 'google' if protocol == 'google' else 'openai' if protocol == 'responses' else 'deepseek'
    if protocol == 'google' or protocol == 'responses' and model == 'auto':
        name = 'KYROZEN_BENCHMARK_GOOGLE_POLICY' if protocol == 'google' else 'KYROZEN_BENCHMARK_RESPONSES_POLICY'
        try:
            policy = json.loads(os.environ[name])
        except (KeyError, TypeError, ValueError):
            raise PermissionError('root native policy is missing or invalid') from None
        if (not isinstance(policy, dict)
                or set(policy) != {'provider', 'model_main', 'model_simple', 'model_complex', 'base_url'}
                or policy['provider'] != provider or policy['model_main'] != 'auto'
                or policy['base_url'] != url or model != 'auto'
                or any(not isinstance(policy[key], str) or not policy[key] for key in ('model_simple', 'model_complex'))
                or policy['model_simple'] == policy['model_complex']):
            raise PermissionError('root native policy does not match its trusted parent')
        return benchmark_provider_config(policy, api_key=api_key, context_window_tokens=context_window_tokens)
    return benchmark_provider_config({'provider': provider, 'api_key': api_key, 'base_url': url,
        'model_simple': model, 'model_complex': model, 'context_window_tokens': context_window_tokens})


def benchmark_google_provider(config, factory):
    """Change only native SDK transport; retain the selected product's provider."""
    from google import genai
    from urllib.parse import urlsplit
    parsed = urlsplit(config.base_url or '')
    if (config.provider != 'google' or parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username):
        raise ValueError('benchmark Google SDK endpoint must be a loopback parent gateway')
    provider = factory(config)
    provider._client.close()
    provider._client = genai.Client(api_key=config.api_key, http_options={
        'base_url': config.base_url, 'api_version': 'v1beta', 'timeout': 180_000,
        'retry_options': {'attempts': 1}})
    return provider


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


def _artifact_references(value):
    """Read framework-owned output envelopes, never prose or provider state."""
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(_artifact_references(item))
        return result
    if not isinstance(value, dict):
        return set()
    if value.get('kind') == 'tool.result' and isinstance(value.get('payload'), dict):
        payload = value['payload']
        name, output, success = payload.get('name'), payload.get('output'), payload.get('is_error') is False
    elif 'receipt_id' in value and isinstance(value.get('action'), str):
        name, output, success = value['action'], value.get('tool_result'), value.get('success') is True
        if not isinstance(output, dict):
            error = value.get('tool_error')
            output = {'data': value.get('result_data'), 'error': error,
                      'metadata': error.get('metadata', {}) if isinstance(error, dict) else {}}
    else:
        return set()
    if not isinstance(output, dict) or not isinstance(output.get('data'), dict):
        return set()
    data = output['data']
    identifier = data.get('artifact_id')
    if not isinstance(identifier, str) or not identifier:
        return set()
    metadata = output.get('metadata')
    # Oversized and archived outputs have a matched machine summary and metadata.
    if (set(data) in ({'artifact_id', 'total_chars'}, {'artifact_id', 'total_chars', 'preview'})
            and type(data['total_chars']) is int and data['total_chars'] >= 0
            and ('preview' not in data or isinstance(data['preview'], str))
            and isinstance(metadata, dict) and metadata.get('artifact_id') == identifier):
        return {identifier}
    # The paging builtin returns its own scoped ID; file contents and tool args
    # containing the same spelling have no artifact authority.
    if (name == 'read_tool_artifact' and success and output.get('error') is None
            and set(data) == {'artifact_id', 'offset', 'limit', 'total_chars', 'content', 'next_offset'}
            and all(type(data[key]) is int for key in ('offset', 'limit', 'total_chars'))
            and data['offset'] >= 0 and 1 <= data['limit'] <= 1000 and data['total_chars'] >= 0
            and isinstance(data['content'], str)
            and (data['next_offset'] is None or type(data['next_offset']) is int
                 and 0 <= data['next_offset'] <= data['total_chars'])):
        return {identifier}
    return set()


class _CanonicalHistoryImport:
    """One invocation's scoped, ordered native journal and referenced artifacts."""
    def __init__(self, store, *, user_id, workspace_id, artifact_workspace_id, budget_id, review, owned_session):
        self.store, self.user_id, self.workspace_id = store, user_id, workspace_id
        self.artifact_workspace_id = artifact_workspace_id
        self.budget_id, self.review, self.owned_session = budget_id, review, owned_session
        self.session_id = None
        self.prefix = []
        self.index, self.previous, self.head = 0, None, None
        self.ids, self.references, self.artifacts = set(), set(), set()
        self.receipt_references = {}
        self.pending = None
        self.finished = False

    def note_receipt(self, session_id, receipt):
        self.receipt_references.setdefault(session_id, set()).update(_artifact_references(receipt))

    @property
    def scope(self):
        return dict(user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)

    @property
    def artifact_scope(self):
        return dict(self.scope, workspace_id=self.artifact_workspace_id)

    def receive(self, *, session_id, kind, offset, chunk, final):
        self.owned_session(session_id)
        if (self.finished or kind not in {'entry', 'artifact', 'finish'} or type(offset) is not int
                or offset < 0 or not isinstance(chunk, str) or len(chunk) > _HISTORY_CHUNK
                or type(final) is not bool):
            raise PermissionError('invalid canonical history chunk')
        if self.session_id is None:
            if (not session_id.startswith(self.budget_id + '_review_') if self.review
                    else session_id != self.budget_id):
                raise PermissionError('canonical history belongs to another invocation phase')
            self.session_id = session_id
            self.prefix = self.store.list_conversation_entries(**self.scope)
            self.head = self.store.conversation_head(**self.scope)
            if (self.review and self.prefix or self.head != (self.prefix[-1]['id'] if self.prefix else None)):
                raise PermissionError('canonical history session is stale or reused')
        if session_id != self.session_id:
            raise PermissionError('canonical history changed its owning session')
        if kind == 'finish':
            self.references.update(self.receipt_references.get(session_id, ()))
            if (self.pending is not None or offset or chunk or not final or self.index < len(self.prefix)
                    or self.previous != self.head or self.store.conversation_head(**self.scope) != self.head
                    or self.artifacts != self.references):
                raise ValueError('canonical history export is incomplete or has dangling artifacts')
            self.finished = True
            return None
        if self.pending is None:
            if offset:
                raise ValueError('canonical history record starts at a nonzero offset')
            self.pending = (kind, [], 0)
        current_kind, pieces, length = self.pending
        if kind != current_kind or offset != length or not chunk:
            raise ValueError('canonical history chunks overlap or are out of order')
        pieces.append(chunk)
        self.pending = (kind, pieces, length + len(chunk))
        if not final:
            return None
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate canonical history object key')
                value[key] = item
            return value
        value = json.loads(''.join(pieces), object_pairs_hook=unique)
        self.pending = None
        if kind == 'entry':
            from openkyrozen.agent.conversation import ConversationEntry
            if (not isinstance(value, dict) or ConversationEntry(**value).as_dict() != value
                    or any(value.get(key) != item for key, item in self.scope.items())
                    or any(value[key] is not None and (not isinstance(value[key], str) or not value[key])
                           for key in ('parent_id', 'turn_id', 'request_id', 'call_id', 'task_id'))
                    or value['parent_id'] != self.previous or value['id'] in self.ids
                    or self.store.conversation_head(**self.scope) != self.head):
                raise PermissionError('canonical entry has a conflicting identity, scope, or parent')
            if self.index < len(self.prefix):
                if value != self.prefix[self.index]:
                    raise ValueError('canonical history conflicts with the existing prefix')
            else:
                self.store.append_conversation_entry(value, expected_head=self.head)
                self.head = value['id']
            self.ids.add(value['id'])
            self.previous = value['id']
            self.index += 1
            self.references.update(_artifact_references(value))
        else:
            self.references.update(self.receipt_references.get(session_id, ()))
            fields = {'id', 'user_id', 'workspace_id', 'session_id', 'content', 'created_at'}
            if (not isinstance(value, dict) or set(value) != fields
                    or any(not isinstance(value[key], str) or not value[key] for key in fields - {'content'})
                    or not isinstance(value['content'], str)
                    or any(value[key] != item for key, item in self.artifact_scope.items())
                    or value['id'] not in self.references or value['id'] in self.artifacts):
                raise PermissionError('artifact is not referenced by this scoped canonical history')
            # The native repository has no import-with-ID API. Keep its exact
            # private table identity; create/get_or_create would change references.
            with self.store._lock, self.store.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                existing = connection.execute('SELECT * FROM tool_artifacts WHERE id=?', (value['id'],)).fetchone()
                if existing is not None and dict(existing) != value:
                    raise ValueError('canonical artifact ID conflicts with an existing artifact')
                if existing is None:
                    columns = ('id', 'user_id', 'workspace_id', 'session_id', 'content', 'created_at')
                    connection.execute('INSERT INTO tool_artifacts(id,user_id,workspace_id,session_id,content,created_at) '
                                       'VALUES(?,?,?,?,?,?)', tuple(value[key] for key in columns))
                connection.commit()
            self.artifacts.add(value['id'])
        return None


def _export_canonical_history(store, coordinator, scopes):
    # Frozen V2 has no canonical journal; keep its original message callbacks.
    if not hasattr(store, 'list_conversation_entries'):
        return
    for scope, artifact_scope in scopes.values():
        entries = store.list_conversation_entries(**scope)
        references = set(coordinator.artifact_references.get(scope['session_id'], ()))
        def send(kind, value):
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            for offset in range(0, len(encoded), _HISTORY_CHUNK):
                end = offset + _HISTORY_CHUNK
                coordinator.call('canonical_history', session_id=scope['session_id'], kind=kind,
                                 offset=offset, chunk=encoded[offset:end], final=end >= len(encoded))
        for entry in entries:
            references.update(_artifact_references(entry))
            send('entry', entry)
        for identifier in sorted(references):
            with store.connection() as connection:
                artifact = connection.execute('SELECT * FROM tool_artifacts WHERE id=? AND user_id=? '
                    'AND workspace_id=? AND session_id=?', (identifier, artifact_scope['user_id'],
                    artifact_scope['workspace_id'], artifact_scope['session_id'])).fetchone()
            if artifact is None:
                raise PermissionError('canonical history references an unowned or missing artifact')
            send('artifact', dict(artifact))
        coordinator.call('canonical_history', session_id=scope['session_id'], kind='finish',
                         offset=0, chunk='', final=True)


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
    runtime._benchmark_child_routing_records = []

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
        if hasattr(parent.memory.store, 'list_conversation_entries'):
            with coordinator.lock:
                run['conversation_workspace_id'] = self.interaction_workspace_id()
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

        canonical = (_CanonicalHistoryImport(parent.memory.store, user_id=parent.memory.user_id,
            workspace_id=self.interaction_workspace_id(), artifact_workspace_id=parent.tasks.workspace_id,
            budget_id=budget_id, review=review,
            owned_session=owned_session) if hasattr(parent.memory.store, 'list_conversation_entries') else None)

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
                session_id = owned_session(values['child_id'])
                if canonical is not None:
                    canonical.note_receipt(session_id, values['receipt'])
                return coordinator.record_receipt(run_id, session_id, values['receipt'])
            if operation == 'canonical_history':
                if canonical is None:
                    raise PermissionError('selected snapshot has no canonical conversation history')
                return canonical.receive(**values)
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
                            if request['op'] == 'canonical_history':
                                raise RuntimeError('canonical history import failed') from exc
                            replies.append({'id': request['id'], 'ok': False,
                                            'error': f'{type(exc).__name__}: {str(exc)[:1000]}'})
                    if response['state'] == 'complete':
                        if replies:
                            raise RuntimeError('actor completed with unanswered coordinator operations')
                        result = _json_copy(response['result'])
                        if (canonical is not None
                                and (not canonical.finished or result.get('session_id') != canonical.session_id)):
                            raise RuntimeError('actor completed without its canonical history')
                        records = result.pop('_benchmark_request_context_records', [])
                        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                            raise ValueError('invalid child context observations')
                        self._benchmark_child_context_records.extend(records)
                        routing_records = result.pop('_benchmark_routing_observations',[])
                        if (not isinstance(routing_records,list) or any(not isinstance(row,dict)
                                or row.get('variant') != getattr(self,'_benchmark_routing_variant',None)
                                for row in routing_records)):
                            raise ValueError('invalid child routing observations')
                        self._benchmark_child_routing_records.extend(routing_records)
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
        self.artifact_references = {}

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
        self.artifact_references.setdefault(child_id, set()).update(_artifact_references(receipt))
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


def run_actor(payload, client, *, source, provider_config, routing_variant=None):
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
    routing = None
    if routing_variant is not None:
        try:
            from v3_routing_supplement import install
        except ModuleNotFoundError:
            from benchmarks.v3_routing_supplement import install
        routing = install(runtime,routing_variant)
    coordinator = _CoordinatorPort(payload, client)
    original = runtime._invoke_delegated
    original_create_session = runtime._create_session
    try:
        from prompt_worker import _ProviderObservations
    except ModuleNotFoundError:
        from benchmarks.prompt_worker import _ProviderObservations
    original_provider = runtime.get_provider
    observations = []
    history_scopes = {}

    def get_provider(config):
        provider = (benchmark_google_provider(config, original_provider) if config.provider == 'google'
                    else original_provider(config))
        observation = _ProviderObservations(runtime, provider)
        observations.append(observation)
        return provider

    runtime.get_provider = get_provider

    def create_session(*args, **kwargs):
        session = original_create_session(*args, **kwargs)
        session_id = session.memory.session_id
        if hasattr(application.store, 'list_conversation_entries'):
            with runtime.use_session(session):
                history = runtime.conversation_history
                history_scopes[session_id] = (
                    dict(user_id=history.user_id, workspace_id=history.workspace_id, session_id=history.session_id),
                    dict(user_id=session.tasks.user_id, workspace_id=session.tasks.workspace_id, session_id=session_id))
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
                return dict(result, _benchmark_request_context_records=records,
                    **({'_benchmark_routing_observations':routing.records} if routing is not None else {}))
            finally:
                runtime._active_interaction_mode.reset(mode_token)
                _export_canonical_history(application.store, coordinator, history_scopes)
                usage = coordinator.run.get('usage', {})
                prefixes = (coordinator.budget_id + '_review_', coordinator.run_id + '_review_')
                coordinator.call('usage', usage={key: value for key, value in usage.items()
                    if (key.startswith(prefixes) if payload['review']
                        else key in {coordinator.run_id, coordinator.budget_id})})
                attempts = application.store.list_usage_attempts(user_id=identity['user_id'], limit=10000)
                coordinator.call('usage_attempts', attempts=attempts)
    finally:
        application.close()
        if routing is not None:
            routing.close()


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
            client = connect_actor_client()
            bootstrap = client.rpc('actor_bootstrap')
            result = run_actor(bootstrap['payload'], client, source=source,
                               provider_config=benchmark_provider_config(bootstrap['provider_config']),
                               routing_variant=bootstrap.get('routing_variant'))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
