# Web, REST, streaming, webhooks, and MCP

`kyrozen-web` serves the browser UI and API through the same runtime, workspace, SQLite store, permission gates, provider configuration, and tool adapters used by the terminal. Bind to loopback for local use:

```sh
kyrozen-web --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/api/health
```

The [generated runtime inventory](tool-inventory.md) is authoritative for every live HTTP method/path, tool, capability label, and MCP input schema. This guide explains common payloads and the shared boundaries. `server:app` remains an import-compatible application object; lifecycle setup is performed when the ASGI application starts.

## Authentication and request rules

Direct loopback requests may be unauthenticated when no server token is configured. When `KYROZEN_SERVER_TOKEN` is set, protected REST and MCP requests require either:

```http
Authorization: Bearer <server-token>
```

or `X-Kyrozen-Token: <server-token>`. The token is compared as a secret. Do not put it in a URL, client-side source, logs, or a public curl example. To bind beyond loopback, always configure it; otherwise direct non-loopback API requests receive HTTP 503. Use HTTPS through a trusted reverse proxy for remote traffic.

The browser login endpoint accepts `POST /api/auth/session` with `{"token":"..."}` and returns `{"authenticated":true}` plus a short-lived HttpOnly, SameSite=Strict cookie when a token is configured. `DELETE /api/auth/session` revokes that in-memory cookie and clears it. This browser session does not issue a bearer token for scripts. If no server token is set, login returns authenticated for local UI startup.

REST handlers expect JSON objects for JSON bodies. Invalid JSON or a non-object body returns 400. Session IDs are at most 128 characters and allow letters, digits, `_`, `.`, `:`, and `-`. Chat messages are limited to 12,000 characters. Profiles are `auto`, `coder`, or `researcher`. The web process assigns one server actor (`KYROZEN_SERVER_ACTOR`); a request body cannot select another private user.

Common errors are HTTP 400 for invalid payloads, 401 for a missing/invalid configured token, 403 for a capability refusal, 404 for a missing scoped record, 409 for stale state or unmet evidence, 413 for oversized input, 429 for bounded webhook registration capacity, 500 for an unexpected handler failure, and 503 for provider unavailability or a non-loopback request without configured server authentication. Individual endpoints can use other status codes; check the response `detail` and the handler contract.

## Chat

`POST /api/chat` accepts a JSON object with required `message` and optional `session_id`, `profile`, `speaker`, `audience`, and `channel`. `profile` defaults to `auto`; an omitted session ID creates one. The handler sanitizes the message, applies interaction controls, runs the session, and persists user/assistant messages and receipts.

Example:

```sh
curl -sS http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","profile":"auto","message":"Explain this project"}'
```

The success body contains `reply`, `session_id`, `profile`, `decision_assist`, `memory_receipt`, `cost`, `interaction`, and `context`. Interaction controls share this endpoint. `mode` selects a supported interaction mode; `question_response` resolves a pending clarification using its `request_id`, `action`, and `answers`; `plan_action` supplies `plan_id`, positive `version`, and `action` (`accept`, `revise`, or `cancel`). A question response and plan action cannot appear together. `system_one_backend` (legacy `fast_backend`) selects `off`, `jev`, or `kev`; `decision_assist_backend` selects its corresponding backend. These controls do not bypass authorization or tool gates. A Jev API key can be supplied for setup but must remain secret.

## Streaming chat

`POST /api/chat/stream` takes the same body and authorization as `/api/chat` and returns `text/event-stream`. Each event is one `data: <JSON>\n\n` record. Implementations may receive:

| `event` | Key data | Meaning |
|---|---|---|
| `content` | `chunk` | Plain response text delta; control/tool prefixes are filtered from user-visible chunks. |
| `tool_receipt` | `tool_receipt` | A completed action receipt. |
| `tasks` | `tasks` | Durable task status. |
| `interaction` | `interaction` | Current question, plan, mode, or approval envelope. |
| `memory_receipt` | `memory_receipt` | Memory usage receipt when available. |
| `usage` | `cost` | Local usage/cost summary. |
| `context` | `context` | Current context status. |
| `fast_decision` | `backend` | System One decision metadata. |
| `error` | `code`, `error` | A stream-time failure; the stream may already have emitted earlier events. |
| `completion` | `status` | Successful turn completion. |

The stream ends with `data: [DONE]`. Clients should handle an `error` event and disconnect before completion as incomplete turns; do not treat an open connection as success. Provider-token accounting is explained in [usage and cost](usage-and-cost.md).

## REST endpoint groups

All protected routes require the authentication above. Exact parameter spelling is in [tool-inventory.md](tool-inventory.md).

| Route group | Operations and effects |
|---|---|
| Health and UI: `/`, `/manifest.json`, `/api/health` | Serve the chat page/PWA manifest or report service health. Health is authenticated when a server token is configured. |
| Cost: `/api/cost`, `/api/cost/reset` | Read usage/cost for a requested supported scope or reset selected local counters. Reset is a destructive local bookkeeping action, not a provider refund. |
| Decisions: `/api/v2/decision-assist`, `/api/v2/system-one/diagnostics`, `/api/v2/fast/diagnostics` | Read backend state and session-scoped decision/usage diagnostics. A diagnostic query can initialize the named session. |
| Memory: `/api/memory`, `/api/v2/memory`, `/api/v2/memory/claims` | Read or create scoped claims and inspect/delete a particular claim. Creation is a durable write. Deletion deactivates the claim and handles dependent learning state; it is not a database wipe. |
| Events: `/api/v2/events` | Read a bounded event page, optionally filtered by event type/session. Results are restricted to this server actor and active workspace. |
| Sessions/history: `/api/v2/sessions`, `/api/v2/sessions/{session_id}`, `/api/v2/sessions/{session_id}/history` | List and read sessions, conversation state, and tree summaries for the configured server actor. |
| History rollback: `/api/v2/sessions/{session_id}/history/{node_id}/rollback` | Requires write capability and body `{"confirm":"rollback"}`; optional `expected_head_id` prevents acting on a stale head. Returns `status`, target `node_id`, a `recovery_node_id`, and `preserved_memory`. Does not reverse outside-world effects. |
| Durable tasks: `/api/v2/tasks`, `/api/v2/tasks/{task_id}/resume` | List tasks optionally by `session_id`, create a task, and resume an existing scoped task. Create requires `description`; optional `session_id`, `dependencies`, `acceptance`, integer `priority`, and a capability-filtered `action` with plain-string `args` set the task contract. |
| Schedules: `/api/v2/schedules`, `/api/v2/schedules/{job_id}/disable` | Read, create, and disable actor/workspace-scoped scheduled work. The web process must be running at the scheduled time. |
| Learning: `/api/v2/learning`, `/api/v2/learning/features`, `/api/v2/learning/metrics`, `/api/v2/learning/provider`, `/api/v2/learning/constitution` | Inspect proposals, feature registry, metrics, or constitution; select `{"mode":"local"}` or `{"mode":"remote"}`. These operations can change persistent learning configuration. |
| Learning evidence: `/api/v2/learning/{proposal_id}/evidence`, `/replay`, `/omission`, `/retire`, `/restore`, `/rollback`, `/capsule` and `/api/v2/learning/capsules` | Inspect/import/export evidence artifacts, record supplied candidate/predecessor or with/without-item arrays, and request gated lifecycle transitions. Retire, restore, import, and rollback change learned state; replay/omission inputs are evidence claims and are not proof that an external test ran. |
| Skills: `/api/v2/skills`, `/api/v2/skills/install`, `/api/v2/skills/{skill_id}/activate`, `/rollback` | List installed skills, install from a `path` with optional boolean `activate`, and change or roll back a skill version. These operations load instruction content and should be limited to trusted paths. |
| Agents: `/api/v2/agents`, `/api/v2/agents/run`, `/api/v2/agents/runs`, `/api/v2/agents/runs/{run_id}`, `/cancel` | List available profiles and scoped runs, submit a task, inspect a run, or request cancellation. Cancellation prevents future tools between action calls; it does not undo completed external effects. |
| Voice: `/api/voice/transcribe`, `/api/voice/speak` | Send/receive media via the optional speech integrations; provider availability and media validation determine which operations can run. |
| Webhooks: `/api/webhooks/register`, `/api/webhooks`, `/api/webhooks/test` | Register a bounded set of public destinations for supported events, inspect process-local registrations, or send a test. See the network caveats below. |

For exact request/response fields on the current routes, start at the handler source in `openkyrozen/interfaces/web/` and the generated route table. The API is intentionally not a promise that every endpoint is available in every capability profile.

## Webhooks

Registration takes `{"url":"https://public.example/hook","events":["chat.completed"]}`. The default event is `chat.completed`; `test` is also supported. The current process accepts at most 32 hooks. URLs must be HTTP(S), have no embedded credentials, and must not target obvious loopback/private hosts or addresses. Delivery sends `{"event":"chat.completed","data":{...}}`; the completion data contains a bounded redacted reply summary, reply length, actor, session ID, profile, and whether the turn streamed.

DNS rebinding is not fully prevented by application checks. Restrict outbound traffic with network policy or an egress proxy when untrusted users can register hooks. Delivery has a timeout and failure is audited without making an otherwise completed chat fail. Registrations are held in the web service process; verify their state after a restart.

## MCP endpoint

`POST /mcp` accepts JSON-RPC and the supported MCP protocol versions `2024-11-05` and `2025-06-18`. Initialize with `method: "initialize"`, then notify `notifications/initialized`. `tools/list` exposes only tools allowed by `KYROZEN_MCP_CAPABILITIES` (default `workspace`); `server/discover` returns the corresponding discovery view. Use `tools/call` with `params.name` and `params.arguments`. The server maps typed objects for common structured tools into the existing plain-string action contract; for all other tools, pass `{"args":"<plain string>"}`.

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"1.0"}}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"}}}
```

Unknown method/tool, malformed params, and denied capabilities return JSON-RPC errors. A tool that ran but returned a failed `ExecutionReceipt` returns a normal tool result with `isError: true`. MCP calls use the normal executor and capability token. MCP is a non-interactive surface: an approval requirement cannot be accepted through a chat prompt on this endpoint. `KYROZEN_MCP_ALLOW_DANGEROUS=1` selects the full profile; only set it for an intentionally trusted deployment.

## Test locally

Use a temporary database and workspace for integration checks so test calls cannot alter personal state. Keep the development server on loopback and do not add real provider credentials to documentation examples. `make docs-check` verifies that inventory rows still match the route registry, the Markdown links resolve, and documented shell commands reference valid Make targets and local endpoints.
