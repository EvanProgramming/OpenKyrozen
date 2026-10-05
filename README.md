<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.12 or 3.13">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-555555" alt="macOS, Linux, and Windows">
  <img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT license">
</p>

<p align="center"><img src="docs/openkyrozen-banner.svg" alt="OpenKyrozen terminal wordmark" width="960"></p>

<h1 align="center">OpenKyrozen</h1>

<p align="center"><strong>A local-first terminal agent for coding, research, and project work, with durable memory and evidence-gated learning.</strong></p>

## Install

The current published stable release is **v2.0.8**. It supports Python 3.12 and 3.13; Python 3.14 is outside the supported range.

macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.8/install.sh | sh
kyrozen
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.8/install.ps1 | iex
kyrozen
```

On first launch, choose a provider and configure its credential. For example, set `DEEPSEEK_API_KEY` in the shell that starts OpenKyrozen. Read the [installation guide](docs/installation.md) before installing from a source checkout, updating, or recovering an interrupted update.

## Try it

```text
You: read this project and explain its architecture
You: investigate the failing tests and prepare a plan
You: accept the plan and fix the test
You: search for the latest Python release and cite the source
```

`kyrozen` opens the global workspace; `kyrozen --project /path/to/repo` binds a session to a project. The web interface is optional: run `kyrozen-web --host 127.0.0.1 --port 8000` and visit `http://127.0.0.1:8000`. Configure `KYROZEN_SERVER_TOKEN` before binding beyond loopback.

The interaction modes make authority visible: Ask and Plan are read/network-only; Agent carries out explicitly requested or accepted work under capability and approval checks. These are application policy controls, not an operating-system sandbox.

## What it does

- Operates on a selected workspace with file, shell, Git, web, browser, and GitHub tools.
- Supports hosted model providers and local Ollama, with simple/complex model defaults and configurable fallback.
- Persists sessions, events, claims, tasks, usage, and learning state in local SQLite. Optional vector and project graphs are derived indexes.
- Delegates suitable work to scoped sub-agents and collects reviewable results.
- Records outcome evidence and promotes bounded learning proposals only after validation. Learning does not fine-tune model weights or grant new capabilities.
- Provides terminal and optional web/API/MCP interfaces over the shared runtime.

Jev Decision is an optional judgment service for bounded routing and evidence checks. It may abstain or fall back; it never executes tools or approves work. Local Kev requires explicit consent. Compact prompts and tool discovery remain optional; the recorded pilot was inconclusive and the default profile is `classic`.

## Documentation

The README is an overview. Detailed installation, command, provider, safety, storage, API, and operating instructions are maintained in the [documentation index](docs/index.md). Start with [usage](docs/usage.md), [configuration](docs/configuration.md), or the [security guide](docs/security.md). The [generated runtime inventory](docs/tool-inventory.md) is the source of truth for tool names, HTTP routes, and MCP schemas.

## Develop

Use Python 3.12 or 3.13. From a checkout, `make install` provisions the full development environment; `make install-core` selects the smaller non-browser path.

```sh
make check
make docs-check
make lint
make test
```

The [development guide](docs/development.md) explains project structure, test profiles, packaging, acceptance checks, and release workflow. Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md); the project is licensed under [MIT](LICENSE).

<p align="center"><sub>English · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></sub></p>
