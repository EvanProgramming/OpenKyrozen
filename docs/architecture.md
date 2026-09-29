# Architecture

OpenKyrozen is a Python agent with a terminal UI, a FastAPI surface, and a shared runtime.

```text
terminal UI / web UI
        │
        ▼
interaction mode and task routing
        │
        ├── Ask / Plan: read and network operations
        └── Agent: approved execution
                    │
                    ▼
             provider + model
                    │
                    ▼
             tool registry / executor
                    │
                    ├── workspace files, shell, Git, web, browser
                    ├── private Graphify project index
                    └── SQLite events, tasks, claims, and memory
                    │
                    ▼
             response, receipts, and learning evidence
```

## Core modules

| Area | Entry point |
| --- | --- |
| Agent loop and terminal behavior | main.py |
| Provider selection and fallback | providers.py |
| Built-in tools and capability labels | tools.py, tool_registry.py |
| Memory and durable events | memory.py, event_store.py |
| Tasks and scheduling | task_engine.py, scheduler.py |
| Learning and artifact lifecycle | learning_engine.py, learning_worker.py |
| Web UI, REST, and streaming | server.py |
| Terminal UI bridge | tui/, tui_backend.py |
| Project graph and bundled skills | project_graph.py, builtin_skills/ |

## Durable state

SQLite is the authoritative store for session events, usage, tasks, claims, and learning artifacts. ChromaDB is a rebuildable semantic index, not the source of truth. Project intelligence mirrors eligible source files into a private path under ~/.kyrozen/v2/graphs/ and never treats the mirrored path as the user's workspace.

## Safety boundaries

- Ask and Plan cannot write files, run commands, mutate Git/browser state, or register dynamic tools.
- Agent execution remains subject to the configured capability upper bound and approval policy.
- Tool output, memory, downloaded pages, and skill text are untrusted data, not permission grants.
- Self-learning can propose and validate artifacts, but cannot silently add capabilities, dynamic tools, or provider credentials.

For the exact live tool and endpoint contract, use the generated inventory at tool-inventory.md. For self-learning details, use self-evolution.md.
