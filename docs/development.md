# Development guide

## Setup

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
```

Use Python 3.12 or 3.13. make install installs the full optional set and Chromium; make install-core is enough for the non-browser path.

## Checks

```bash
make check
make lint
make docs-check
make test
```

Useful focused commands:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
make benchmark
make wheel-smoke
make docker-smoke
```

tool-inventory.md is generated from the live tool and FastAPI registries. Run venv/bin/python scripts/generate_tool_inventory.py --write after changing a tool, endpoint, or MCP schema.

## Project layout

```text
main.py                 agent loop and CLI behavior
server.py               FastAPI web server and API
providers.py            provider registry and fallback behavior
tools.py                built-in tool implementations
memory.py               local memory facade
event_store.py          durable SQLite events
learning_engine.py      evidence-gated learning lifecycle
tui/                    Bubble Tea terminal UI
prompts/                role and instruction templates
plugins/                hook-based extensions
builtin_skills/         bundled skill packages
tests/                  unittest suites
docs/                   operator, API, benchmark, and comparison docs
```

## Documentation rules

- Keep the root README short and task-oriented.
- Put detailed behavior in one focused document, then link it from the README.
- Treat generated inventories as generated; do not hand-edit them.
- Label historical measurements and avoid general performance claims from small samples.
- Do not commit venv/, chroma_memory/, build artifacts, generated package metadata, credentials, or private runtime data.

See AGENTS.md for naming, testing, security, and commit conventions.
