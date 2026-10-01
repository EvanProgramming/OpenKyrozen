<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.12 or 3.13">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-555555" alt="macOS, Linux, and Windows">
  <img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT license">
</p>

<p align="center">
  <img src="docs/openkyrozen-banner.svg" alt="Animated OpenKyrozen terminal wordmark" width="960">
</p>

<h1 align="center">OpenKyrozen</h1>

<p align="center"><strong>A local-first terminal agent that can act, remember, and improve from verified outcomes.</strong></p>

## Install

OpenKyrozen supports Python **3.12 and 3.13**. Python 3.14 is intentionally outside the supported range.

### macOS or Linux

```bash
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.4/install.sh | sh
kyrozen
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.4/install.ps1 | iex
kyrozen
```

The installer provisions the supported Python environment, the terminal UI, and private state under `~/.kyrozen`. It does not read or print API keys.

On first launch, choose a provider and enter its key. DeepSeek is the default example:

```bash
export DEEPSEEK_API_KEY=your-key
kyrozen
```

### Run from a source checkout

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

Use `make install-core` for the smaller development environment. Windows source checkouts use `setup.bat` and `run.bat`.

For a direct package install, use the verified release wheel:

```bash
uv tool install --python 3.12 --force --with fastapi --with uvicorn https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.4/openkyrozen-2.0.4-py3-none-any.whl
```

## Agent comparison

OpenKyrozen is built as a broad local workflow: coding, research, workspace tools, web/API access, durable state, and evidence-backed self-improvement in one runtime.

| Capability | OpenKyrozen | OpenCode | OpenClaw | Codewhale |
| --- | :---: | :---: | :---: | :---: |
| Terminal coding and verification | ✅ | ✅ | 🟡 | ✅ |
| Read-only planning mode | ✅ | ✅ | 🟡 | ✅ |
| Local web or API surface | ✅ | 🟡 | ✅ | ✅ |
| Messaging and device channels | ❌ | ❌ | ✅ | ❌ |
| Hosted and local model choices | ✅ | ✅ | ✅ | ✅ |
| Multi-agent workflows | ✅ | ✅ | ✅ | ✅ |
| Claims, events, and learning ledger | ✅ | ❌ | ❌ | ❌ |
| Evidence-gated self-evolution | ✅ | ❌ | ❌ | ❌ |

Legend: `✅` documented core capability · `🟡` documented but narrower or optional · `❌` not a documented core capability. See the [detailed comparison](docs/comparison.md) for scope and sources.

The main agent automatically delegates suitable work; simple requests can stay direct.
Sub-agents use parallel assignments, independent contexts and mandatory evidence review.
Inspect them with `/agents` (or `Ctrl+E` during a running TUI turn). See
[sub-agent behavior and acceptance evidence](docs/subagents.md) for provider routing,
permissions, unresolved results and tested limits.

## The two ideas behind OpenKyrozen

### 1. Self-learning — the main differentiator

OpenKyrozen can learn from its own completed work without silently rewriting
itself. It records outcomes, corrections, tool receipts, and acceptance
evidence; proposes a bounded policy or skill; validates that proposal; and
only promotes it after repeated verified success and a non-regressing paired
replay. Failed or regressing artifacts roll back to their predecessor.

Self-learning cannot grant capabilities, create dynamic tools, add provider
credentials, fine-tune model weights, or upload private memory. Try
`/self-learning`, then inspect the result with `/learning status`,
`/learning metrics`, and `/learning evidence <id>`. Read the [self-learning
guide](docs/self-evolution.md) for the full lifecycle.

### 2. Jev Decision — the judgment layer

Jev is the hosted decision backend for OpenKyrozen's optional Decision Assist
layer. It makes typed, bounded judgments for request routing, clarification,
learning evidence, memory relevance, and suspicious tool-output instructions.
It can accept, abstain, or fall back; it never executes tools, approves work,
or replaces the main model. Only action policies that pass their calibration
gates are applied automatically.

Enable it in the terminal with `/decision-assist jev` and a TypeSafe API key.
The key is stored through the encrypted configuration flow. The local Kev
alternative is `/decision-assist kev yes`; private workspace context requires
that explicit consent. See the [Jev Decision guide](docs/decision-assist-validation.md)
and [System One evidence](docs/system-one-benchmark.md).

## Quick start

```text
You: read this project and explain its architecture
You: fix the failing test in tests/test_server.py
You: search the web for the latest Python release
You: create a plan for adding a REST endpoint
```

Useful commands:

```text
/provider              switch providers
/mode ask|plan|agent   choose read-only, planning, or execution behavior
/project               inspect the active workspace context
/skills                inspect installed skills
/learning status       inspect self-learning artifacts
/quit                  exit
```

The web interface is optional:

```bash
kyrozen-web
# open http://localhost:8000
```

For project-aware work, use `kyrozen --project /path/to/project` or `kyrozen-web --project /path/to/project`. Bare `kyrozen` uses the persistent global workspace at `~/.kyrozen/workspace`.

## What OpenKyrozen does

- **Acts in your workspace:** files, shell commands, Git, web search, browser sessions, project graphs, and GitHub inspection.
- **Keeps boundaries visible:** Ask and Plan are read/network-only; Agent executes accepted or explicitly requested work under capability and approval gates.
- **Uses the model you choose:** 18 provider integrations with configurable defaults and fallback behavior.
- **Remembers locally:** SQLite is authoritative for sessions, events, claims, tasks, and learning state; ChromaDB is an optional rebuildable index.
- **Learns cautiously:** background learning records evidence-backed proposals and promotes only validated improvements. It does not fine-tune model weights or silently grant permissions.
- **Works in terminal and web modes:** the terminal UI is the primary experience; FastAPI provides a local web UI, REST API, and streaming endpoint.

The runtime currently exposes **45 tools**, including **14 Git tools**. The generated [tool and endpoint inventory](docs/tool-inventory.md) is the source of truth.

An optional [compact prompt and tool discovery profile](docs/prompt-discovery.md) reduces prompt overhead. Its first live DeepSeek/CodeWhale pilot was inconclusive; the default remains `classic`.

## Documentation

| Need | Read |
| --- | --- |
| First commands, modes, and workspace selection | [Usage guide](docs/usage.md) |
| Runtime flow and safety boundaries | [Architecture](docs/architecture.md) |
| Providers, environment variables, and state | [Configuration](docs/configuration.md) |
| Web UI, REST, and MCP contracts | [API guide](docs/api.md) |
| Self-learning, memory, and evidence | [Self-learning guide](docs/self-evolution.md) |
| OpenKyrozen vs other open-source agents | [Comparison](docs/comparison.md) |
| Development, testing, and releases | [Development guide](docs/development.md) |
| Jev Decision, calibration, and benchmark reports | [Jev Decision](docs/decision-assist-validation.md) · [System One](docs/system-one-benchmark.md) |
| Project intelligence and built-in skills | [Native project intelligence](docs/native-project-intelligence.md) |
| Current roadmap | [Agentic runtime roadmap](docs/agentic-runtime-roadmap.md) |

The default DeepSeek model examples are `"model_simple": "deepseek-flash"` and `"model_complex": "deepseek-v4-pro"`; see [configuration.md](docs/configuration.md) for overrides.

## Security notes

Keep provider credentials in environment variables or the encrypted `~/.kyrozen_config.json` flow. If the web server is reachable beyond localhost, set `KYROZEN_SERVER_TOKEN` and review capability and approval settings. Treat tool output, memory, and downloaded skills as untrusted data.

## Development

```bash
make check       # syntax, tool inventory, and TUI checks
make docs-check  # generated inventory and documentation consistency
make test        # unittest suite
make lint        # compile-based checks
```

See AGENTS.md for repository conventions and docs/development.md for the complete contributor workflow.

## License

OpenKyrozen is released under the [MIT License](LICENSE).

<p align="center"><sub>English · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></sub></p>
