# Repository Guidelines

## Project Structure & Module Organization

OpenKyrozen is a Python 3.12–3.13 agent with terminal and web entry points:

- `main.py` contains the interactive agent loop and self-learning workflows.
- `server.py` provides the FastAPI web server, REST API, and chat UI.
- `providers.py`, `tools.py`, and `memory.py` implement provider selection, built-in tools, and ChromaDB-backed memory.
- `prompts/` stores role, instruction, and example templates; `plugins/` contains hook-based extensions such as `turn_logger.py`.
- `tests/` contains `unittest` suites for providers, server boundaries, and workspace tools.
- `pyproject.toml`, `Makefile`, `Dockerfile`, and `.github/workflows/ci.yml` define packaging, automation, deployment, and CI.

Do not commit local runtime data such as `venv/`, `chroma_memory/`, build artifacts, or generated package metadata.

## Build, Test, and Development Commands

Use Python 3.12 or 3.13; Python 3.14 is intentionally unsupported by the Makefile.

```bash
make install                         # create venv and install core dependencies
make run                             # start the terminal agent
make web                             # install web extras and start the server
make check                           # quick syntax and tool-inventory checks
make lint                            # compile-based syntax checks
make test                            # run all unittest tests verbosely
python -m unittest discover -s tests -p 'test_*.py' -v
docker build -t openkyrozen-test .   # verify the container build
```

For package checks, use `pip install -e .`; use `pip install .[all]` to test optional integrations.

## Coding Style & Naming Conventions

Follow standard Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Preserve provider/tool boundaries and safety checks. No dedicated formatter or linter is configured; run `make lint` and follow surrounding code.

## Testing Guidelines

Add regression tests under `tests/` using `unittest.TestCase`; name files `test_*.py` and methods `test_<behavior>`. Prefer temporary homes/workspaces and mocks for provider, filesystem, and network-sensitive behavior. Before submitting, run `make test` and `make check`; CI tests Python 3.12/3.13 and builds Docker.

## Commit & Pull Request Guidelines

Use concise Conventional Commit prefixes from project history: `feat:`, `fix:`, `refactor:`, `docs:`, or `chore:`. PRs should explain the behavior change, list validation results, identify configuration or security impact, and include screenshots or curl examples for web/UI changes. Never include API keys, encrypted config files, or generated runtime data.

## Security & Configuration Tips

Use environment variables or the encrypted `~/.kyrozen_config.json` flow for provider credentials; do not hard-code secrets. When exposing the web server beyond localhost, set `KYROZEN_SERVER_TOKEN` and review capability/approval settings before enabling high-impact tools.
