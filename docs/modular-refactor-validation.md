# Modular refactor validation

Validation date: 2026-09-30. Results cover the modular refactor included with this record.
The unrelated, untracked `AGENTS.md` was preserved.

## Implementation

The `openkyrozen` package contains the implementations, grouped by the responsibilities
in [architecture](architecture.md). Root executable scripts are launch-only wrappers.

| Former entry point | Before | After |
| --- | ---: | ---: |
| `main.py` | 8,185 lines | 5 lines |
| `server.py` | 2,691 lines | 6 lines |
| `tui_backend.py` | 1,310 lines | 5 lines |
| Go `tui/main.go` | 2,598 lines | 41 lines |

The largest Python implementation module is 604 lines. `AgentRuntime` owns explicitly
supplied session/workspace services and turn contexts; its turn coordinator delegates
preparation, response recovery, action rounds and completion. Web requests retain their
existing serialization while using explicit sessions. Tool execution shares capability
and approval gates and `ExecutionReceipt`. SQL resides in feature repositories. Core
orchestration consumes injected history, rendering, installation and worker operations.

The SQLite schema initializer, embedded web template and all 18 provider metadata
entries were compared against the original source and are unchanged. Provider transport
adapters, console command names, ASGI alias, JSONL shapes and durable scope rules remain.

## Results

| Check | Result | Evidence |
| --- | --- | --- |
| Python 3.12.13 `make test` | PASS | 339 tests, 53.695 seconds, no skips |
| Python 3.13.14 `make test` | PASS | 339 tests, 49.511 seconds, no skips |
| `make check` and Go 1.27.1 tests | PASS | Architecture, syntax, tool inventory, shell checks and `go test ./...` |
| `make lint`, `make docs-check` | PASS | Both supported Python versions; 39 runtime tools and 53 HTTP/MCP endpoints |
| Agent workflow acceptance | PASS | Read-only Plan inspection, proposal/acceptance, real file writes and localhost deployment verification |
| Clean wheel and sdist installation | PASS | Separate virtual environments on Python 3.12 and 3.13, outside the checkout |
| Installed runtime contracts | PASS | Four console commands, resources, JSONL backend startup, ASGI alias, authenticated HTTP startup and detached-worker module execution |
| Rebuilt TUI interaction | PASS | PTY with real backend: onboarding, conversations, chat switching, denied/approved Git reset in a temporary repository; 60x20, 160x45 and 80x24 resizing |
| Chromium web integration | PASS | Authentication, sessions, streaming and session persistence after restart |
| Memory/index integration | PASS | Chroma indexing plus authoritative-store fallback and index rebuild tests |
| Architecture regressions | PASS | Import cycles, legacy imports, forbidden adapter dependencies, inert package imports, actor/session/project isolation and concurrent execution |
| Deterministic learning benchmark | PASS | Fixture runner and output contract; no new provider performance claim |
| `git diff --check` | PASS | No whitespace errors |
| Docker build and replace-container persistence | BLOCKED | Docker CLI/engine is unavailable on this host; `make docker-smoke` reports this prerequisite |

Agent and TUI acceptance use deterministic provider responses. Filesystem, Git, local
HTTP, browser and persistence effects are exercised with production implementations in
isolated workspaces. Provider transport and fallback behavior have fixture-based
regression coverage. No live provider performance or API availability claim is made.

Overall runtime acceptance remains partial only for the required Docker check. Run
`make docker-smoke` on a Docker-equipped host to complete that remaining acceptance item.

## Reproduction

```bash
make test
make check
make lint
make docs-check
make agent-acceptance
make tui-acceptance
make wheel-smoke
make benchmark
make docker-smoke
```

`make test` enables browser integration. Provision matching Chromium with
`python -m playwright install chromium`; network access and localhost sockets must be
available. To select an isolated Python 3.13 environment without replacing the repository
venv, pass both `PYTHON=/path/to/python` and `VENV_PYTHON=/path/to/python` to Make.
Go must be on `PATH` for checks and TUI acceptance. The installation smoke script uses
an already installed `uv` when available and otherwise uses pip.
