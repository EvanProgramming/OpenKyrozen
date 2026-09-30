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
