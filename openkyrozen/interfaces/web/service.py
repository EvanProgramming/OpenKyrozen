"""Web adapter with explicit, injectable application ownership."""
from __future__ import annotations
import os
import re
import threading
from importlib import resources
from fastapi import FastAPI, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from openkyrozen.tasks.scheduler import JobScheduler

class WebService:
    def __init__(self, application=None):
        self._owns_application = application is None
        self._application = application
        self._scheduler_instance = None
        self.app = FastAPI(title="OpenKyrozen API", description="Self-learning AI Agent — REST API + Web Chat", version="2.0.4")
        self._SERVER_TOKEN = os.environ.get("KYROZEN_SERVER_TOKEN", "").strip()

        self._BROWSER_AUTH_COOKIE = "openkyrozen_browser_session"

        self._BROWSER_AUTH_TTL_SECONDS = 3600

        self._browser_auth_sessions: dict[str, float] = {}

        self._browser_auth_lock = threading.RLock()

        self._CONFIGURED_SERVER_ACTOR = os.environ.get("KYROZEN_SERVER_ACTOR", "local").strip()

        self._SERVER_ACTOR_ID = (self._CONFIGURED_SERVER_ACTOR
                           if re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", self._CONFIGURED_SERVER_ACTOR)
                           else "local")

        self._LOCAL_CLIENT_NAMES = {"localhost", "127.0.0.1", "::1", "testclient"}

        self._sessions: dict[str, dict[str, Any]] = {}  # session_id -> {messages, user_id, created}

        self._chat_lock = threading.RLock()

        self._MAX_SESSION_MESSAGES = 32

        self._MAX_MESSAGE_CHARS = 12_000

        self._MAX_MEMORY_QUERY_CHARS = 500

        self._MAX_MEMORY_RESULTS = 50



        self._MCP_PROTOCOL_VERSION = "2025-06-18"

        self._MCP_SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-06-18"}

        self._MCP_SERVER_INFO = {"name": "openkyrozen", "version": self.app.version}

        self._webhooks: list[dict] = []

        self._MAX_WEBHOOKS = 32

        self._ALLOWED_WEBHOOK_EVENTS = {"chat.completed", "test"}

        self._WEBHOOK_SUMMARY_CHARS = 500

        self.app.post('/api/auth/session')(self.api_auth_session)
        self.app.delete('/api/auth/session')(self.api_auth_session_logout)
        self.app.get('/', response_class=HTMLResponse)(self.chat_page)
        self.app.get('/api/v2/system-one/diagnostics', dependencies=[Depends(self.require_api_access)])(self.fast_diagnostics)
        self.app.get('/api/v2/fast/diagnostics', dependencies=[Depends(self.require_api_access)])(self.fast_diagnostics)
        self.app.get('/api/v2/decision-assist', dependencies=[Depends(self.require_api_access)])(self.decision_assist_status)
        self.app.post('/api/chat', dependencies=[Depends(self.require_api_access)])(self.api_chat)
        self.app.post('/api/chat/stream', dependencies=[Depends(self.require_api_access)])(self.api_chat_stream)
        self.app.get('/api/memory', dependencies=[Depends(self.require_api_access)])(self.api_memory)
        self.app.get('/api/v2/memory', dependencies=[Depends(self.require_api_access)])(self.api_v2_memory)
        self.app.get('/api/v2/memory/claims', dependencies=[Depends(self.require_api_access)])(self.api_v2_memory_claims)
        self.app.post('/api/v2/memory/claims', dependencies=[Depends(self.require_api_access)])(self.api_v2_create_memory_claim)
        self.app.get('/api/v2/memory/claims/{claim_id}', dependencies=[Depends(self.require_api_access)])(self.api_v2_memory_claim)
        self.app.delete('/api/v2/memory/claims/{claim_id}', dependencies=[Depends(self.require_api_access)])(self.api_v2_memory_forget_claim)
        self.app.get('/api/v2/tasks', dependencies=[Depends(self.require_api_access)])(self.api_v2_tasks)
        self.app.post('/api/v2/tasks', dependencies=[Depends(self.require_api_access)])(self.api_v2_create_task)
        self.app.post('/api/v2/tasks/{task_id}/resume', dependencies=[Depends(self.require_api_access)])(self.api_v2_resume_task)
        self.app.get('/api/v2/learning', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning)
        self.app.get('/api/v2/learning/metrics', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_metrics)
        self.app.get('/api/v2/learning/features', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_features)
        self.app.post('/api/v2/learning/provider', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_provider)
        self.app.get('/api/v2/learning/{proposal_id}/evidence', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_evidence)
        self.app.post('/api/v2/learning/{proposal_id}/replay', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_replay)
        self.app.post('/api/v2/learning/{proposal_id}/omission', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_omission)
        self.app.post('/api/v2/learning/{proposal_id}/retire', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_retire)
        self.app.post('/api/v2/learning/{proposal_id}/restore', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_restore)
        self.app.get('/api/v2/learning/{proposal_id}/capsule', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_export_capsule)
        self.app.post('/api/v2/learning/capsules', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_import_capsule)
        self.app.get('/api/v2/learning/constitution', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_constitution)
        self.app.post('/api/v2/learning/{proposal_id}/rollback', dependencies=[Depends(self.require_api_access)])(self.api_v2_learning_rollback)
        self.app.get('/api/v2/events', dependencies=[Depends(self.require_api_access)])(self.api_v2_events)
        self.app.get('/api/v2/sessions/{session_id}/history', dependencies=[Depends(self.require_api_access)])(self.api_v2_session_history)
        self.app.post('/api/v2/sessions/{session_id}/history/{node_id}/rollback', dependencies=[Depends(self.require_api_access)])(self.api_v2_session_history_rollback)
        self.app.get('/api/v2/sessions', dependencies=[Depends(self.require_api_access)])(self.api_v2_sessions)
        self.app.get('/api/v2/sessions/{session_id}', dependencies=[Depends(self.require_api_access)])(self.api_v2_session)
        self.app.get('/api/v2/schedules', dependencies=[Depends(self.require_api_access)])(self.api_v2_schedules)
        self.app.post('/api/v2/schedules', dependencies=[Depends(self.require_api_access)])(self.api_v2_create_schedule)
        self.app.post('/api/v2/schedules/{job_id}/disable', dependencies=[Depends(self.require_api_access)])(self.api_v2_disable_schedule)
        self.app.get('/api/v2/skills', dependencies=[Depends(self.require_api_access)])(self.api_v2_skills)
        self.app.post('/api/v2/skills/install', dependencies=[Depends(self.require_api_access)])(self.api_v2_install_skill)
        self.app.post('/api/v2/skills/{skill_id}/activate', dependencies=[Depends(self.require_api_access)])(self.api_v2_activate_skill)
        self.app.post('/api/v2/skills/{skill_id}/rollback', dependencies=[Depends(self.require_api_access)])(self.api_v2_rollback_skill)
        self.app.get('/api/v2/agents', dependencies=[Depends(self.require_api_access)])(self.api_v2_agents)
        self.app.post('/api/v2/agents/run', dependencies=[Depends(self.require_api_access)])(self.api_v2_run_agent)
        self.app.get('/api/cost', dependencies=[Depends(self.require_api_access)])(self.api_cost)
        self.app.post('/api/cost/reset', dependencies=[Depends(self.require_api_access)])(self.api_cost_reset)
        self.app.get('/api/health', dependencies=[Depends(self.require_api_access)])(self.api_health)
        self.app.post('/mcp', dependencies=[Depends(self.require_api_access)])(self.mcp_endpoint)
        self.app.post('/api/voice/transcribe', dependencies=[Depends(self.require_api_access)])(self.api_transcribe)
        self.app.get('/api/voice/speak', dependencies=[Depends(self.require_api_access)])(self.api_speak)
        self.app.post('/api/webhooks/register', dependencies=[Depends(self.require_api_access)])(self.register_webhook)
        self.app.get('/api/webhooks', dependencies=[Depends(self.require_api_access)])(self.list_webhooks)
        self.app.post('/api/webhooks/test', dependencies=[Depends(self.require_api_access)])(self.test_webhook)
        self.app.get('/manifest.json')(self.pwa_manifest)
        self.app.on_event('startup')(self.startup)
        self.app.on_event('shutdown')(self.shutdown)

    @property
    def application(self):
        if self._application is None:
            from openkyrozen.app.bootstrap import build_application
            self._application = build_application(surface="web", user_id=self._SERVER_ACTOR_ID)
        return self._application

    @property
    def _agent(self):
        return self.application.runtime

    @property
    def _scheduler(self):
        if self._scheduler_instance is None:
            self._scheduler_instance = JobScheduler(self._agent.memory_bank.store, workspace_id=self._agent.memory_bank.workspace_id, user_id=self._SERVER_ACTOR_ID)
            for name in ("learning_cycle", "chat", "task_worker"):
                self._scheduler_instance.register_callback(name, self._run_scheduled_job)
        return self._scheduler_instance

    @property
    def CHAT_HTML(self):
        return resources.files("openkyrozen.interfaces.web").joinpath("templates/chat.html").read_text(encoding="utf-8")

    from openkyrozen.interfaces.web.auth import (_issue_browser_auth_session, _browser_auth_session_valid, _is_loopback_client, require_api_access, _sanitize_api_message, _audit_log_path, _audit, api_auth_session, api_auth_session_logout)

    from openkyrozen.interfaces.web.tasks import (_execute_durable_task, _task_manager, _task_worker, _task_scopes, _recover_task_scopes, _run_task_worker_cycle, _run_scheduled_job, api_v2_tasks, api_v2_create_task, api_v2_resume_task, api_v2_schedules, api_v2_create_schedule, api_v2_disable_schedule)

    from openkyrozen.interfaces.web.chat import (_normalise_session_id, _normalise_profile, _normalise_memory_actor, _normalise_memory_context, _initialise_session_defaults, _set_memory_context, _actor_for_request, _get_or_create_session, _interaction_for_session, _history_for_session, _ensure_history_baseline, _public_history_node, _apply_chat_controls, _run_session_chat, _validate_message, _server_capabilities, _allowed_server_tools, _cost_summary, _cost_report, _json_object, api_chat, api_chat_stream)

    from openkyrozen.interfaces.web.pages import (chat_page, fast_diagnostics, decision_assist_status, pwa_manifest)

    from openkyrozen.interfaces.web.memory import (api_memory, api_v2_memory, api_v2_memory_claims, api_v2_create_memory_claim, api_v2_memory_claim, api_v2_memory_forget_claim)

    from openkyrozen.interfaces.web.learning import (api_v2_learning, api_v2_learning_metrics, api_v2_learning_features, api_v2_learning_provider, api_v2_learning_evidence, api_v2_learning_replay, api_v2_learning_omission, api_v2_learning_retire, api_v2_learning_restore, api_v2_learning_export_capsule, api_v2_learning_import_capsule, api_v2_learning_constitution, api_v2_learning_rollback, api_v2_skills, api_v2_install_skill, api_v2_activate_skill, api_v2_rollback_skill, api_v2_agents, api_v2_run_agent)

    from openkyrozen.interfaces.web.history import (api_v2_events, api_v2_session_history, api_v2_session_history_rollback, api_v2_sessions, api_v2_session)

    from openkyrozen.interfaces.web.usage import (api_cost, api_cost_reset, api_health)

    from openkyrozen.interfaces.mcp.server import (_mcp_response, _mcp_error, _mcp_tool_schema, _mcp_tool_descriptors, _mcp_string_arguments, mcp_endpoint)

    from openkyrozen.interfaces.web.media import (api_transcribe, api_speak)

    from openkyrozen.interfaces.web.webhooks import (_redact_webhook_value, _chat_completed_payload, _emit_chat_completed, _validate_webhook_url, register_webhook, list_webhooks, _fire_webhooks, test_webhook)

    from openkyrozen.interfaces.web.lifecycle import (_load_plugins, _trigger_hook, startup, shutdown, _server_parser, _parse_server_args, main_entry)
