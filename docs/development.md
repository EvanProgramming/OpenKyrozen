# Development guide

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the issue, contribution, and pull request workflow.

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

`make docs-check` compares the generated runtime inventory to live registries,
checks local documentation paths/fragments and navigation coverage, compares
README and website installer-version references, and validates documented Make
targets and loopback HTTP examples. `tests/test_docs.py` covers broken links,
fragments, nested relative paths, index coverage, and installer-version drift.

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

GitHub Actions runs the Python 3.12 core suite and the full Python 3.13 suite for application or test changes. It selects TUI, wheel, Docker, and installed-upgrade checks from the files changed. Documentation-only changes run inventory and documentation checks. Unknown paths and CI configuration changes run the full check set. A scheduled Monday run and manual dispatch run full browser-enabled suites on Python 3.12 and 3.13.

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

See [AGENTS.md](../AGENTS.md) for naming, testing, security, and commit conventions. The [documentation index](index.md) links the operator and product guides.

## GitHub labels and releases

`.github/labels.yml` is the canonical list of issue labels, including their colors and descriptions. When setting up or changing repository labels, create or update them manually in GitHub from that list; no label-sync workflow is configured. Issue forms apply their matching labels.

Release tags are maintainer-created. Use `v` followed by the exact `project.version` in `pyproject.toml`; pushing a `v*` tag starts the existing release workflow, which checks that the versions match before building.

All internal Python imports use `openkyrozen.*`. Keep the four console command names
and root executable launches stable. Use `build_application(memory=..., tools=...)`
or `create_app(application=...)` to inject test dependencies; do not patch process
globals. See [architecture](architecture.md) for ownership and dependency rules.
