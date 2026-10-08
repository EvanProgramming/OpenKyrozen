# Architecture

OpenKyrozen is a modular monolith. Its Python package contains the implementations;
root scripts are launchers. The Bubble Tea client remains in the `tui` Go package.

```text
CLI / TUI JSONL / web REST and SSE / MCP
                  │
                  ▼
       AgentRuntime.chat(session, message)
                  │
   preparation → response recovery → action rounds → completion
                  │
                  ▼
       capability and approval gates → ExecutionReceipt
                  │
                  ▼
       provider / workspace tools / repository ports
                  │
                  ▼
       SDKs / subprocess / browser / SQLite / optional Chroma
```

## Composition and ownership

`openkyrozen.app.bootstrap.build_application` constructs storage, adapters, feature
services and the runtime. `Application.close` releases owned worker and browser resources.
Importing core modules or `server:app` creates no database, client, worker or terminal UI.
The web factory `openkyrozen.interfaces.web.app.create_app(application=None)` accepts an
injected application, or creates one during startup. Web chat and MCP retain request
serialization while using explicit sessions; they do not swap agent globals.

| Owner | State |
| --- | --- |
| Application | Configuration, authoritative store, provider configuration, shared usage and feature services |
| Workspace | Root, bound tool adapters, graph, GitHub client and launch context |
| AgentSession | Actor, conversation, durable task scope, interaction controller, learning feedback and subagents |
| Turn | Response state, callbacks, capability token, selected model and request usage |

Session identity includes actor, interaction scope, session ID and workspace root.
Personal memory retains its existing global workspace scope. Source snapshots and
project history use the source scope. Changing projects selects another adapter owner;
existing sessions retain their original workspace. Context variables carry session,
event, approval and execution state through provider threads without global mutation.

## Package responsibilities

| Package | Responsibility |
| --- | --- |
| app | Configuration, resources, composition and lifecycle |
| agent | Session runtime, coordinated turn phases, parsing, prompts, planning, execution, modes and subagents |
| routing | Complexity/model routing, System One choices, Decision Assist, calibration and optional transports |
| providers | Existing provider contract, metadata registry, transport adapters, fallback and usage |
| tools | Workspace-bound filesystem, shell, Git, web, browser, graph and GitHub adapters |
| tasks | Models, evidence-based completion, recovery, workers and scheduling |
| memory | Retrieval, scoped claims and the optional vector-index adapter |
| learning | Feature dispatch and implementations, evidence, candidates, canaries, promotion, rollback and detached worker |
| security | Capabilities, permissions, command protections, credentials and untrusted input |
| workspace | Launch context, project graph and history |
| persistence | SQLite connections/schema and repositories for events, history, tasks, memory, learning, usage and catalogs |
| skills, plugins | Existing loading, registry and lifecycle behavior |
| updates | Verified downloads, package updates and Go/TUI installation |
| interfaces | Rich CLI, Python JSONL backend, web feature routes/templates and MCP |

Compatible providers share the OpenAI transport; native Responses, Anthropic, Google,
Azure, Bedrock, Perplexity and Ollama transports retain their existing contracts.
The Go UI splits its model, startup, events, update/input handling, navigation,
settings, layout and views inside the same package and Bubble Tea model.

## V3 subsystem ownership contract

This contract covers [V3 foundation issue #227](https://github.com/EvanProgramming/OpenKyrozen/issues/227),
requirements R236–R249. Each row names one package owner, not a new class to scaffold.
The existing components are the migration starting points on V2; the linked issues
implement the V3 behavior. In particular, native AgentEngine execution, separate
Working/Strategic Plans, typed provider responses and typed runtime events are
future work, not capabilities delivered by this ownership change.

| Requirement / subsystem | Package owner and existing components | Responsibility and authority boundary | V3 implementation |
| --- | --- | --- | --- |
| R236 AgentEngine | `agent`: `runtime.AgentRuntime`, turn phases in `preparation`, `action_rounds`, `completion` | Sole foreground model/tool loop coordinator; calls provider, planning and executor contracts. Does not translate provider wire formats or bypass authorization. | [#238](https://github.com/EvanProgramming/OpenKyrozen/issues/238) |
| R237 PlanningService | `tasks`: `engine.TaskManager`, `models`, `ports.TaskStore`; `agent.planner` currently bridges textual plans | Owns Working Plan and Strategic Plan transitions, validation and completion evidence. Working Plans are mutable; Strategic Plans require user approval and material revisions require reapproval. Storage persists transitions; the engine requests them. | [#241](https://github.com/EvanProgramming/OpenKyrozen/issues/241), [#242](https://github.com/EvanProgramming/OpenKyrozen/issues/242), [#245](https://github.com/EvanProgramming/OpenKyrozen/issues/245), [#246](https://github.com/EvanProgramming/OpenKyrozen/issues/246) |
| R238 ToolRegistry | `tools`: `manifest.ToolRegistry`, `ToolManifest`, `registry`, `adapters.ToolAdapters` | Owns definitions, schemas, metadata and executor registration. Supplies the catalog to providers and execution; registration or discovery never grants permission. Extend this registry rather than adding a competing catalog. | [#232](https://github.com/EvanProgramming/OpenKyrozen/issues/232) |
| R239 ToolExecutor | `agent`: `executor`, `types.ExecutionReceipt` | Owns authorized invocation and receipts. Uses registry definitions, security decisions and bound tool implementations; denied or failed calls cannot become successful effects. Does not own permission policy or plan transitions. | [#233](https://github.com/EvanProgramming/OpenKyrozen/issues/233), [#236](https://github.com/EvanProgramming/OpenKyrozen/issues/236) |
| R240 SecurityPolicy | `security`: `capabilities`, `tool_policy`, `permissions`, `permission_gate` | Owns capability and approval authorization. Effective permissions stay within configured, session and delegated bounds; model output, routing advice and catalog entries cannot approve a call. The executor enforces the decision. | [#261](https://github.com/EvanProgramming/OpenKyrozen/issues/261) |
| R241 ProviderAdapter | `providers`: `base.LLMProvider`, provider transport modules, `factory`, `calls` | Owns native request/response translation, streaming and provider errors. Receives conversation/tool contracts; returns model results and usage. Never executes model-requested tools or mutates plans. | [#228](https://github.com/EvanProgramming/OpenKyrozen/issues/228), [#229](https://github.com/EvanProgramming/OpenKyrozen/issues/229), [#230](https://github.com/EvanProgramming/OpenKyrozen/issues/230), [#231](https://github.com/EvanProgramming/OpenKyrozen/issues/231) |
| R242 ContextManager | `agent`: `models.AgentSession`, `context`, `compaction.ContextState` | Owns active conversation representation and compaction. Uses provider token/usage information and plan snapshots; preserves call/result identity and active plans. Does not promote memories or independently change plan state. | [#247](https://github.com/EvanProgramming/OpenKyrozen/issues/247), [#248](https://github.com/EvanProgramming/OpenKyrozen/issues/248) |
| R243 Task/Plan Store | `persistence`: `tasks.TasksRepository`, `events.EventsRepository`, `store.EventStore`, `database` | Owns durable task/plan records and revision events. Implements feature-owned storage ports and atomic writes/migrations. PlanningService owns transition semantics; storage cannot infer user approval or completion from prose. | [#244](https://github.com/EvanProgramming/OpenKyrozen/issues/244), [#264](https://github.com/EvanProgramming/OpenKyrozen/issues/264) |
| R244 RoutingService | `routing`: `router`, `policy`, `models`, `choices` | Owns model/provider/reasoning/resource selection. Reads provider capabilities, complexity and resource bounds; returns a routing decision. Cannot grant capabilities, invoke tools or mark tasks complete. | [#251](https://github.com/EvanProgramming/OpenKyrozen/issues/251) |
| R245 SubagentManager | `agent`: `subagents.SubAgentManager`, `delegation`, `delegation_runtime` | Owns child orchestration, budgets, isolation and lifecycle. Uses the shared engine/executor and security bounds; child authority can only narrow parent authority. Results require evidence/review before parent acceptance. | [#254](https://github.com/EvanProgramming/OpenKyrozen/issues/254), [#258](https://github.com/EvanProgramming/OpenKyrozen/issues/258) |
| R246 MemoryService | `memory`: `service.MemoryBank`, `retrieval`, `claims`, `ports` | Owns retrieval/storage semantics and scoped claims. Uses injected SQLite/vector ports; Chroma remains derived. Retrieved content is evidence/context, never execution authority or an approved plan. | [#252](https://github.com/EvanProgramming/OpenKyrozen/issues/252) |
| R247 LearningService | `learning`: `engine.LearningEngine`, `dispatcher`, evidence/evaluation/promotion modules | Owns post-turn evidence, evaluation and promotion. Uses memory, skill and storage contracts; promotion requires evidence. Learning cannot change foreground execution authority or approve its own tool calls. | [#253](https://github.com/EvanProgramming/OpenKyrozen/issues/253) |
| R248 Jev/System One | `routing`: `system_one`, `decision_assist`, `transport`, `kev` | Advisory only: proposes routing/ranking/review choices within RoutingService. Never owns execution authority, permission approval, plan acceptance or completion. Remote/local transports stay behind injected boundaries. | [#251](https://github.com/EvanProgramming/OpenKyrozen/issues/251) |
| R249 Interfaces | `interfaces`: CLI, TUI JSONL, web REST/SSE and MCP adapters; `tui` renders the Go client | Consume/render runtime events and submit user input, approvals and cancellation. Do not implement model/tool loops, plan transitions or completion rules. Typed event migration replaces existing surface shapes through explicit adapters. | [#255](https://github.com/EvanProgramming/OpenKyrozen/issues/255), [#256](https://github.com/EvanProgramming/OpenKyrozen/issues/256) |

### Dependency direction and enforcement

`app.bootstrap` is the composition root: it constructs concrete adapters and injects
feature services, callbacks and ports. Existing method binding is retained during
migration; it is not a license to move semantic decisions into presentation code.
Interfaces call the runtime/service contracts; feature code emits events through
`agent.ports.EventSink` and requests approval through `Approval`, rather than importing
interface implementations. Concrete provider, tool, SQLite, vector and routing
transports remain behind the injected boundaries.

Permitted feature dependencies follow the responsibilities above: `agent` coordinates
`tasks`, `tools` catalog/model contracts, `security`, provider contracts, `routing`,
`memory` and `learning`; `tasks` owns its models/storage ports; `memory` owns memory
models/storage/vector ports; `learning` consumes memory, skill and storage contracts.
Shared persistence models are allowed; concrete repositories belong at composition.
`tools` uses security/contracts and adapter-specific libraries inside adapter modules;
`providers` uses provider configuration/contracts and SDKs inside transport modules;
`routing` reads provider metadata and security/resource bounds; `persistence` implements
storage without importing interfaces or coordinating agent execution. New dependencies
must preserve these authority limits and avoid import-time cycles.

`scripts/check_architecture.py` enforces the existing static import policy across
`agent`, `tasks`, `memory` and `learning`: no legacy root implementation imports, no
interface package-root or descendant imports, and no listed concrete adapters or
external adapter libraries. Absolute and relative imports are checked, including
function-local imports and concrete adapters reached through statically named import
re-exports (including aliased and chained re-exports). Existing entry adapters (`memory.vector`, `learning.worker`
and `learning.benchmark`) retain their explicit exemption from core adapter bans.
Import-time statement bodies (including branches, try/else/finally, with, loops,
match and class bodies) contribute cycle edges. Function bodies and type-only
`TYPE_CHECKING` / `typing.TYPE_CHECKING` branches do not; their runtime `else`
branches still do. `check(root=...)` accepts a temporary source root for
regressions; `check()` and the script entry point still check this repository.

This is a static import guard, not an authority proof: it does not inspect dynamic
imports, dynamically assigned exports, injected callables or every semantic
dependency listed above. Runtime safety
and ownership tests remain necessary. Each linked implementation issue must extend
contract/authority tests as it migrates behavior; this issue does not claim the whole
V3 architecture is already implemented.

## Ports and durable data

The inbound chat contract is
`AgentRuntime.chat(session, message, *, clear_tasks=False, profile=None,
memory_context=None, on_event=None, approve=None) -> str`.
Providers keep `LLMProvider.chat/chat_stream`. Small feature-owned protocols describe
memory/vector, task, learning, scheduling, history and interaction storage; event and
approval boundaries are callables. Concrete adapters are supplied at composition.
Foreground, durable, MCP and subagent actions share the executor and produce the same
`ExecutionReceipt`; external response shapes remain unchanged.

SQLite remains authoritative. Its existing schema version, tables, IDs, locking,
conditional task claims and history head checks are retained. Task writes and learning
fingerprints live in repositories. Chroma is a derived index: retrieval falls back to
SQLite on failure and the index can be rebuilt without losing durable records.
Claim implementations belong to memory; learning retains delegating methods for callers.

## Safety and validation

Ask and Plan retain read/network capability intersections. Fast and Decision Assist
cannot approve execution or expand capabilities. Tool output, memory, downloaded pages
and skill text remain untrusted. Learning promotion and rollback retain evidence gates,
and detached workers retain their scope and singleton ownership.

`make check` includes package dependency checks for import cycles, legacy internal
imports and forbidden core-to-interface/adapter dependencies. `tests/test_architecture.py`
also checks inert imports, actor/session/workspace isolation, concurrent turn ownership,
and vector fallback/rebuild. Run the full Python and Go suites, workflow acceptance,
installed wheel/sdist smoke and Docker persistence smoke as documented in
[development](development.md). A blocked check is not a pass. See the [refactor validation record](modular-refactor-validation.md) for this migration’s results.

See [tool inventory](tool-inventory.md), [self-learning](self-evolution.md) and
[Jev Decision](decision-assist-validation.md) for product contracts.
