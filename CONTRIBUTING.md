# Contributing to OpenKyrozen

Thanks for helping improve OpenKyrozen. Bug reports, documentation fixes, focused code changes, and well-supported feature proposals are welcome. Start with the [documentation index](docs/index.md), and follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Start with an issue

Search existing issues before opening one. Use the matching GitHub form for bugs, feature requests, documentation, or questions. For a large or behavior-changing proposal, describe the problem and intended outcome in an issue first so maintainers can discuss scope before implementation. Issues labeled `good first issue` or `help wanted` are suitable places to start.

Never include API keys, access tokens, encrypted configuration, private runtime data, or unredacted logs in an issue or pull request. Use [private vulnerability reporting](SECURITY.md) for security issues.

## Set up the project

Use Python 3.12 or 3.13. From a checkout, install the full development environment with:

```bash
make install
```

For the non-browser development path, use `make install-core`. See the [development guide](docs/development.md) for project structure and additional checks.

## Make a focused change

- Work on a branch and keep the change focused on one issue or improvement.
- Follow the existing Python style and preserve the project's provider, tool, and security boundaries.
- Add or update documentation when behavior, configuration, commands, or public interfaces change.
- `docs/tool-inventory.md` is generated. After changing a tool, endpoint, or MCP schema, run `venv/bin/python scripts/generate_tool_inventory.py --write` and include the generated result.
- Keep local runtime data, `venv/`, build output, credentials, and generated package metadata out of commits.

Use the checks that cover your change. The normal project checks are:

```bash
make check
make docs-check
make lint
make test
```

For a focused regression, run the relevant test file or acceptance target listed in the [development guide](docs/development.md). Report checks you ran and any that you could not run.

## Open a pull request

- Explain the problem, the change, and any user-visible behavior or compatibility impact.
- Link the related issue and list validation commands with their results.
- Call out configuration or security effects. For web or UI changes, include a screenshot or a redacted curl example where useful.
- Keep commits readable and use the project's Conventional Commit prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, or `chore:`.

Maintainers create release tags. The release workflow expects a `v`-prefixed tag whose version exactly matches `project.version` in `pyproject.toml`; see the [development guide](docs/development.md).
