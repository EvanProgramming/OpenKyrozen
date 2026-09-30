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
make agent-acceptance
make tui-test
make tui-acceptance
make docker-smoke
```

tool-inventory.md is generated from the live tool and FastAPI registries. Run venv/bin/python scripts/generate_tool_inventory.py --write after changing a tool, endpoint, or MCP schema.

## Project layout

```text
openkyrozen/app/         dependency construction and configuration
openkyrozen/agent/       runtime, sessions and coordinated turn phases
openkyrozen/providers/   provider contracts and transport adapters
openkyrozen/tools/       workspace-bound tool adapters
openkyrozen/persistence/ SQLite and feature repositories
openkyrozen/memory/      retrieval, claims and vector adapter
openkyrozen/tasks/       task models, completion, recovery and workers
openkyrozen/learning/    feature implementations and artifact lifecycle
openkyrozen/interfaces/ CLI, JSONL backend, web and MCP adapters
main.py, server.py       launch-only compatibility scripts
other root *.py          executable compatibility launchers
routing/security/workspace/skills/plugins/updates live under openkyrozen/
tui/                    Bubble Tea model, input, events, layout and views
prompts/, plugins/      templates and example extensions
builtin_skills/          bundled skill packages
tests/, docs/            regression suites and documentation
```

## Documentation rules

- Keep the root README short and task-oriented.
- Put detailed behavior in one focused document, then link it from the README.
- Treat generated inventories as generated; do not hand-edit them.
- Label historical measurements and avoid general performance claims from small samples.
- Do not commit venv/, chroma_memory/, build artifacts, generated package metadata, credentials, or private runtime data.

See AGENTS.md for naming, testing, security, and commit conventions.

All internal Python imports use `openkyrozen.*`. Keep the four console command names
and root executable launches stable. Use `build_application(memory=..., tools=...)`
or `create_app(application=...)` to inject test dependencies; do not patch process
globals. See [architecture](architecture.md) for ownership and dependency rules.
