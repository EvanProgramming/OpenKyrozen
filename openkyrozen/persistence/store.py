from __future__ import annotations

from typing import Any

"""SQLite persistence adapter composed of responsibility-specific repositories."""
from .database import SQLiteDatabase
from .models import utc_now, stable_hash
from .events import EventsRepository
from .history import HistoryRepository
from .tasks import TasksRepository
from .usage import UsageRepository
from .memory import MemoryRepository
from .learning import LearningRepository
from .catalogs import CatalogsRepository
from .deletion import DeletionRepository

class EventStore:
    def __init__(self, path=None):
        self.database = SQLiteDatabase(path)
        self.path = self.database.path
        self._lock = self.database._lock
        self.connection = self.database.connection
        self.events = EventsRepository(self.database)
        self.history = HistoryRepository(self.database)
        self.tasks = TasksRepository(self.database)
        self.usage = UsageRepository(self.database)
        self.memory = MemoryRepository(self.database)
        self.learning = LearningRepository(self.database)
        self.catalogs = CatalogsRepository(self.database)
        self.deletion = DeletionRepository(self.database)
    _json = staticmethod(SQLiteDatabase._json)
    _loads = staticmethod(SQLiteDatabase._loads)

    def append_event(self, event_type: str, payload: Any, *, user_id: str='local', workspace_id: str='default', session_id: str | None=None, task_id: str | None=None):
        return self.events.append_event(event_type, payload, user_id=user_id, workspace_id=workspace_id, session_id=session_id, task_id=task_id)

    def delete_session_data(self, *, user_id: str, workspace_id: str, session_id: str):
        return self.deletion.delete_session(user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def delete_workspace_data(self, *, user_id: str, workspace_id: str, registry_workspace_id: str):
        return self.deletion.delete_workspace(user_id=user_id, workspace_id=workspace_id,
                                              registry_workspace_id=registry_workspace_id)

    def list_events(self, event_type: str | None=None, *, limit: int=100, workspace_id: str='default', session_id: str | None=None, user_id: str | None=None):
        return self.events.list_events(event_type, limit=limit, workspace_id=workspace_id, session_id=session_id, user_id=user_id)

    def list_sessions(self, *, workspace_id: str='default', limit: int=100, user_id: str | None=None):
        return self.events.list_sessions(workspace_id=workspace_id, limit=limit, user_id=user_id)

    def history_head(self, *, user_id: str='local', workspace_id: str='default', session_id: str):
        return self.history.history_head(user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def history_node(self, node_id: str, *, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        return self.history.history_node(node_id, user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def list_history_nodes(self, *, user_id: str='local', workspace_id: str='default', session_id: str, include_recovery: bool=False, limit: int=10000):
        return self.history.list_history_nodes(user_id=user_id, workspace_id=workspace_id, session_id=session_id, include_recovery=include_recovery, limit=limit)

    def list_history_sessions(self, *, user_id: str='local', workspace_id: str='default', limit: int=100):
        return self.history.list_history_sessions(user_id=user_id, workspace_id=workspace_id, limit=limit)

    def insert_history_node(self, node: dict[str, Any], *, set_head: bool=False, expected_head: str | None=None):
        return self.history.insert_history_node(node, set_head=set_head, expected_head=expected_head)

    def set_history_head(self, node_id: str, *, user_id: str='local', workspace_id: str='default', session_id: str, expected_head: str | None=None):
        return self.history.set_history_head(node_id, user_id=user_id, workspace_id=workspace_id, session_id=session_id, expected_head=expected_head)

    def replace_tasks(self, tasks: list[dict[str, Any]], *, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        return self.tasks.replace_tasks(tasks, user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def list_tasks(self, *, workspace_id: str='default', session_id: str | None=None, statuses: set[str] | None=None, limit: int=1000, user_id: str | None=None):
        return self.tasks.list_tasks(workspace_id=workspace_id, session_id=session_id, statuses=statuses, limit=limit, user_id=user_id)

    def record_usage_attempt(self, *, attempt_id: str, provider: str, model: str, surface: str, user_id: str='local', workspace_id: str='default', session_id: str | None=None, run_id: str | None=None, cache_status: str='unknown', prompt_tokens: int | None=None, cache_hit_tokens: int | None=None, cache_miss_tokens: int | None=None, completion_tokens: int | None=None, reasoning_tokens: int | None=None, latency_ms: int | None=None, completion_state: str='completed', usage_status: str='unknown', cost_picos: int | None=None, pricing_snapshot: dict[str, Any] | None=None):
        return self.usage.record_usage_attempt(attempt_id=attempt_id, provider=provider, model=model, surface=surface, user_id=user_id, workspace_id=workspace_id, session_id=session_id, run_id=run_id, cache_status=cache_status, prompt_tokens=prompt_tokens, cache_hit_tokens=cache_hit_tokens, cache_miss_tokens=cache_miss_tokens, completion_tokens=completion_tokens, reasoning_tokens=reasoning_tokens, latency_ms=latency_ms, completion_state=completion_state, usage_status=usage_status, cost_picos=cost_picos, pricing_snapshot=pricing_snapshot)

    def list_usage_attempts(self, *, workspace_id: str | None=None, session_id: str | None=None, user_id: str | None=None, run_id: str | None=None, limit: int=1000):
        return self.usage.list_usage_attempts(workspace_id=workspace_id, session_id=session_id, user_id=user_id, run_id=run_id, limit=limit)

    def usage_totals(self, *, workspace_id: str | None=None, session_id: str | None=None, user_id: str | None=None, run_id: str | None=None, since: str | None=None):
        return self.usage.usage_totals(workspace_id=workspace_id, session_id=session_id, user_id=user_id, run_id=run_id, since=since)

    def create_usage_reset(self, *, scope: str, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        return self.usage.create_usage_reset(scope=scope, user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def latest_usage_reset(self, *, scope: str, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        return self.usage.latest_usage_reset(scope=scope, user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def upsert_memory(self, content: str, *, kind: str='episodic', status: str='active', confidence: float=0.5, user_id: str='local', workspace_id: str='default', session_id: str | None=None, source_event_ids: list[str] | None=None, metadata: dict[str, Any] | None=None):
        return self.memory.upsert_memory(content, kind=kind, status=status, confidence=confidence, user_id=user_id, workspace_id=workspace_id, session_id=session_id, source_event_ids=source_event_ids, metadata=metadata)

    def list_memories(self, *, kind: str | None=None, status: str | None='active', limit: int=100, workspace_id: str='default', session_id: str | None=None, user_id: str | None=None):
        return self.memory.list_memories(kind=kind, status=status, limit=limit, workspace_id=workspace_id, session_id=session_id, user_id=user_id)

    def set_memory_status(self, memory_id: str, status: str, *, workspace_id: str='default', user_id: str | None=None):
        return self.memory.set_memory_status(memory_id, status, workspace_id=workspace_id, user_id=user_id)

    def delete_memories(self, ids: list[str], *, workspace_id: str='default', user_id: str | None=None):
        return self.memory.delete_memories(ids, workspace_id=workspace_id, user_id=user_id)

    def upsert_file(self, rel_path: str, content: str, *, user_id: str='local', workspace_id: str='default'):
        return self.memory.upsert_file(rel_path, content, user_id=user_id, workspace_id=workspace_id)

    def remove_stale_files(self, valid_paths: set[str], *, workspace_id: str='default', user_id: str | None=None):
        return self.memory.remove_stale_files(valid_paths, workspace_id=workspace_id, user_id=user_id)

    def create_proposal(self, kind: str, content: str, *, confidence: float=0.0, evidence: list[str] | None=None, workspace_id: str='default', user_id: str='local'):
        return self.learning.create_proposal(kind, content, confidence=confidence, evidence=evidence, workspace_id=workspace_id, user_id=user_id)

    def update_proposal(self, proposal_id: str, *, status: str, validation: dict[str, Any] | None=None, confidence: float | None=None, workspace_id: str | None=None, user_id: str | None=None):
        return self.learning.update_proposal(proposal_id, status=status, validation=validation, confidence=confidence, workspace_id=workspace_id, user_id=user_id)

    def add_proposal_evidence(self, proposal_id: str, evidence_id: str):
        return self.learning.add_proposal_evidence(proposal_id, evidence_id)

    def list_learning_feature_flags(self, *, user_id: str='local', workspace_id: str='default'):
        return self.learning.list_learning_feature_flags(user_id=user_id, workspace_id=workspace_id)

    def set_learning_feature_flag(self, name: str, enabled: bool, *, user_id: str='local', workspace_id: str='default'):
        return self.learning.set_learning_feature_flag(name, enabled, user_id=user_id, workspace_id=workspace_id)

    def get_learning_policy(self, *, user_id: str='local', workspace_id: str='default'):
        return self.learning.get_learning_policy(user_id=user_id, workspace_id=workspace_id)

    def set_learning_policy(self, mode: str, *, user_id: str='local', workspace_id: str='default'):
        return self.learning.set_learning_policy(mode, user_id=user_id, workspace_id=workspace_id)

    def get_learning_runtime(self, *, user_id: str='local', workspace_id: str='default'):
        return self.learning.get_learning_runtime(user_id=user_id, workspace_id=workspace_id)

    def set_learning_runtime(self, mode: str, status: str, *, model: str='', detail: str='', user_id: str='local', workspace_id: str='default'):
        return self.learning.set_learning_runtime(mode, status, model=model, detail=detail, user_id=user_id, workspace_id=workspace_id)

    def list_proposals(self, *, status: str | None=None, workspace_id: str='default', limit: int=100, user_id: str | None=None):
        return self.learning.list_proposals(status=status, workspace_id=workspace_id, limit=limit, user_id=user_id)

    def upsert_schedule(self, job_id: str, name: str, *, next_run_at: str, interval_seconds: float | None=None, run_at: str | None=None, payload: dict[str, Any] | None=None, enabled: bool=True, workspace_id: str='default', user_id: str='local'):
        return self.catalogs.upsert_schedule(job_id, name, next_run_at=next_run_at, interval_seconds=interval_seconds, run_at=run_at, payload=payload, enabled=enabled, workspace_id=workspace_id, user_id=user_id)

    def list_schedules(self, *, workspace_id: str='default', enabled: bool | None=None, limit: int=500, user_id: str | None=None):
        return self.catalogs.list_schedules(workspace_id=workspace_id, enabled=enabled, limit=limit, user_id=user_id)

    def claim_due_schedules(self, *, now: str, workspace_id: str='default', limit: int=20, user_id: str | None=None):
        return self.catalogs.claim_due_schedules(now=now, workspace_id=workspace_id, limit=limit, user_id=user_id)

    def set_schedule_enabled(self, job_id: str, enabled: bool, *, workspace_id: str='default', user_id: str | None=None):
        return self.catalogs.set_schedule_enabled(job_id, enabled, workspace_id=workspace_id, user_id=user_id)

    def upsert_skill(self, *, name: str, version: str, path: str, description: str, permissions: list[str], status: str='candidate', source: str='local', manifest: dict[str, Any] | None=None, workspace_id: str='default'):
        return self.catalogs.upsert_skill(name=name, version=version, path=path, description=description, permissions=permissions, status=status, source=source, manifest=manifest, workspace_id=workspace_id)

    def list_skills(self, *, workspace_id: str='default', status: str | None=None, limit: int=500):
        return self.catalogs.list_skills(workspace_id=workspace_id, status=status, limit=limit)

    def set_skill_status(self, skill_id: str, status: str, *, workspace_id: str='default'):
        return self.catalogs.set_skill_status(skill_id, status, workspace_id=workspace_id)

    def write_task(self, task, *, user_id, workspace_id, session_id):
        return self.tasks.write_task(task, user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def claim_task(self, *, user_id, workspace_id, session_id):
        return self.tasks.claim_task(user_id=user_id, workspace_id=workspace_id, session_id=session_id)

    def learning_state_fingerprint(self, *, user_id, workspace_id, session_id, file_scope_id):
        return self.learning.state_fingerprint(user_id=user_id, workspace_id=workspace_id, session_id=session_id, file_scope_id=file_scope_id)
