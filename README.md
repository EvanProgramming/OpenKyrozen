<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek%20%7C%20OpenAI%20%7C%20Claude%20%7C%20Gemini%20%7C%20Ollama-API-green?logo=openai" alt="Multi-Provider">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
  <img src="https://img.shields.io/badge/CI-passing-brightgreen" alt="CI">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>Self‑learning AI Agent — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>
<p align="center">A terminal-native AI agent that <em>learns from verified outcomes</em>,<br>operates your filesystem, manages git, fixes bugs, and improves itself over time.</p>

<p align="center">
  🌐 <strong>English</strong> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

---

## 📑 Table of Contents

- [What is OpenKyrozen?](#what-is-openkyrozen)
- [🚀 Installation](#-installation)
  - [Prerequisites](#prerequisites)
  - [Option A: From source](#option-a-from-source-recommended)
  - [Option B: pip install](#option-b-pip-install-from-local-directory)
- [📖 Usage Guide](#-usage-guide)
  - [Terminal mode](#terminal-mode)
  - [In-chat commands](#in-chat-commands)
  - [Web UI mode](#web-ui-mode)
- [🏗 Architecture](#-architecture)
  - [Task complexity routing](#task-complexity-routing)
  - [Model auto-selection](#model-auto-selection)
  - [Provider management](#provider-management)
- [🛠 Tools Reference](#-tools-reference)
  - [File & System](#file--system)
  - [Web](#web)
  - [Git](#git-14-tools)
  - [Memory](#memory)
- [🧠 Dedicated Workflows](#-dedicated-workflows)
  - [Bug fixing](#bug-fixing-6-step-protocol)
  - [Git operations](#git-operations-safety-first)
  - [Complex tasks](#complex-tasks-never-stops-early)
- [🧬 Self-Learning System](#-self-learning-system)
  - [Operational self-evolution and memory guide](docs/self-evolution.md)
- [🌐 Web UI & REST API](#-web-ui--rest-api)
- [🔌 Plugin System](#-plugin-system)
- [🔐 Security](#-security)
- [⚙️ Configuration Reference](#️-configuration-reference)
- [🔧 Development](#-development)
- [📁 Project Structure](#-project-structure)
- [🙏 Standing on the Shoulders of Giants](#-standing-on-the-shoulders-of-giants)
- [📄 License](#-license)

---

## What is OpenKyrozen?

OpenKyrozen is a **self-learning AI agent** that runs in your terminal. Unlike a typical chatbot, it:

- **Uses 31 runtime tools** — 29 base file/shell/web/git/browser actions plus two SQLite memory actions
- **Learns continuously** — background learning creates evidence-backed proposals and only promotes repeated or validated improvements
- **Works with any LLM** — DeepSeek, OpenAI, Claude, Gemini, or local Ollama models
- **Runs on any OS** — macOS, Linux, and Windows (with automatic terminal capability detection)
- **Has a Web UI** — browser-based chat interface with REST API for integration

Think of it as an AI teammate that gets smarter every time you use it.

---

## 🚀 Installation

### Prerequisites

- **Python 3.12 or 3.13** (Python 3.14+ has a known import issue with the OpenAI SDK)
- An API key from any supported provider:

| Provider | Get a key | Cost |
|----------|-----------|------|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com) | ~$0.27/M input tokens |
| **OpenAI** | [platform.openai.com](https://platform.openai.com) | ~$2.50/M input tokens |
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com) | ~$3.00/M input tokens |
| **Google (Gemini)** | [aistudio.google.com](https://aistudio.google.com) | ~$0.15/M input tokens |
| **Ollama** | [ollama.com](https://ollama.com) | Free (runs locally) |

### Option A: From source (recommended)

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen

# macOS / Linux
make install
make run

# Windows
setup.bat
run.bat
```

### Option B: pip install from local directory

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
pip install .

# Then run anywhere:
kyrozen          # terminal agent
kyrozen-web      # web server
```

> **Note:** `pip install openkyrozen` from PyPI is coming soon. For now, install from the local directory or clone the repo.

On first launch, you'll be prompted for an API key. The agent auto-detects your provider and saves the encrypted key to `~/.kyrozen_config.json`.

---

## 📖 Usage Guide

### Terminal mode

Once launched, you'll see the banner and a `You:` prompt. Type naturally — the agent understands plain English (and Chinese, Japanese, Korean).

```text
You: read the README and tell me what this project does
You: create a new Python file called hello.py that prints "Hello World"
You: search the web for the latest Python release date
You: fix the bug in main.py around line 200
You: commit all changes with a good message
```

Kyrozen will:
1. Classify your request (simple / medium / complex)
2. Choose the best model for the job
3. Create a plan if needed
4. Execute tools step by step
5. Show progress in a live task panel
6. Summarize what was done

### In-chat commands

| Command | What it does |
|---------|-------------|
| `/quit` or `/exit` | Exit the agent |
| `/provider` | Switch to a different LLM provider (interactive menu) |
| `/api_key` | Change your API key |
| `/learn` | Immediately scan project files into memory |
| `/forget` | Show recent learnings; `/forget keyword` to delete bad learnings |
| `/update` | Pull the latest version from git |
| `/agent auto\|coder\|researcher` | Choose automatic routing or an isolated learning profile |
| `/learning status [profile]` | Show candidate, canary, active, retired, and rolled-back artifacts |
| `/learning metrics [profile]` | Show verified completion, corrections, errors, cost, and latency metrics |
| `/learning evidence <id>` | Print the proof card, replay, applicability, and outcome receipts |
| `/learning explain <id>` | Print the complete proposal record |
| `/learning replay <id>` | Explain the API-only paired replay workflow (does not execute commands) |
| `/learning rollback <id>` | Roll back a learned artifact or proposal |
| `/memory why <claim-id>` | Inspect a typed claim and its provenance |
| `/memory forget <claim-id>` | Forget a claim and deactivate solely dependent learned artifacts |
| `/self-learning` | Toggle individual self-learning features on/off |

### Web UI mode

```bash
python server.py --port 8000
# Open http://localhost:8000

# For LAN or container access, set a token and bind explicitly:
KYROZEN_SERVER_TOKEN=change-me python server.py --host 0.0.0.0 --port 8000

# Or via Docker:
docker build -t openkyrozen .
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... -e KYROZEN_SERVER_TOKEN=change-me openkyrozen
```

The web interface provides a dark-themed chat UI with real-time streaming, cost tracking, and session management.

---

## 🏗 Architecture

```
User Input
    │
    ▼
┌─────────────────┐
│  Task Classifier │──► simple / medium / complex
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Model Selector  │──► deepseek-chat / deepseek-reasoner / gpt-4o / claude / gemini / llama
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   LLM Provider   │──► 5 backends with automatic fallback chain
└────────┬────────┘
         │  Response + tool calls
         ▼
┌─────────────────┐
│  Tool Executor   │──► 31 runtime tools (file I/O, shell, git, web, memory, browser)
└────────┬────────┘
         │  Tool results fed back to LLM
         │  (up to 50 tool-call rounds per turn)
         ▼
┌─────────────────┐
│     Response     │──► User sees answer + task summary
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Self-Learning   │──► Background: extract facts, score memories, build knowledge graph
└─────────────────┘
```

### Task complexity routing

Kyrozen automatically classifies every request and adapts its behavior:

| Level | Example triggers | Agent behavior |
|-------|-----------------|---------------|
| **Simple** | "hi", "what is Python", "thanks" | Direct reply, zero planning overhead |
| **Medium** | "list files and read README" | Creates a numbered Plan, executes tools sequentially |
| **Complex** | "audit this repo", "fix the bug and commit", "build a web app" | Full Plan → TaskList → progress tracking → never stops early |

### Model auto-selection

The agent picks different models for simple vs complex tasks. You can override these:

```bash
export KYROZEN_MODEL_SIMPLE=deepseek-chat
export KYROZEN_MODEL_COMPLEX=deepseek-reasoner
```

| Provider | Simple tasks (default) | Complex tasks (default) |
|----------|----------------------|------------------------|
| DeepSeek | `deepseek-chat` | `deepseek-reasoner` |
| OpenAI | `gpt-4o` | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |
| Google | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Ollama | `llama3.2` | `llama3.2` |

### Provider management

Switch providers anytime — in chat with `/provider`, or via environment:

```bash
export KYROZEN_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

If the primary provider fails, Kyrozen automatically falls back through a chain (e.g., DeepSeek → OpenAI → Claude). Rate-limit errors (HTTP 429) trigger exponential backoff with jitter.

---

## 🛠 Tools Reference

All 31 runtime tools accept a plain-string `args` field in a JSON action block:

```json
{"action": "read_file", "args": "README.md"}
```

Short aliases work too — `bash`, `cmd`, `sh` → `run_cmd`; `status`, `diff`, `log` → `git_status`, etc.

### File & System

| Tool | Description | Example |
|------|-------------|---------|
| `read_file` | Read file contents | `"README.md"` |
| `write_file` | Create or overwrite a file | `"path\|content"` |
| `list_dir` | List directory contents | `"."` |
| `list_tree` | Recursive directory tree | `"src/"` |
| `find_files` | Glob-based file search | `"*.py\|."` |
| `run_cmd` | Execute shell command | `"python --version"` |

`run_cmd` prepends the `bin` directory of the Python interpreter running
OpenKyrozen, so bare `python` and `pip` resolve inside the active virtual
environment even when the parent shell was not activated. Explicit interpreter
paths such as `/usr/bin/python3` are left unchanged.

### Web

| Tool | Description | Example |
|------|-------------|---------|
| `search_web` | Internet search (Google → DDG → Wikipedia) | `"latest Python release"` |
| `read_webpage` | Fetch URL text content | `"https://example.com"` |

### Git (14 tools)

| Tool | What it does |
|------|-------------|
| `git_status` | Show working tree status |
| `git_diff` | Unstaged / staged / between-commit diffs |
| `git_log` | Commit history (`--oneline --decorate`) |
| `git_branch` | List / create / delete branches |
| `git_add` | Stage files for commit |
| `git_commit` | Commit with message |
| `git_push` / `git_pull` | Remote sync |
| `git_checkout` | Switch branches or restore files |
| `git_stash` | Stash / pop / list working changes |
| `git_reset` | Reset HEAD (`--soft` safe, `--hard` warns) |
| `git_show` | Inspect a commit with `--stat` |
| `git_remote` | List / add / remove remotes |
| `git_clone` | Clone a repository |
| `analyze_remote_repo` | Clone + read all files → structured summary |

### Memory

| Tool | Description |
|------|-------------|
| `search_memory` | Semantic search over stored knowledge |
| `check_stored_data` | Memory statistics and recent facts |

---

## 🧠 Dedicated Workflows

### Bug fixing (6-step protocol)

When you paste an error or traceback, Kyrozen activates automatically:

1. **Reproduce** — read the offending code, re-run the failing command
2. **Diagnose** — parse the traceback, identify root cause
3. **Hypothesise** — state the fix before making any changes
4. **Fix** — apply the minimal code change
5. **Verify** — re-run the failing command; loop back to step 2 if it fails
6. **Explain** — tell you what was wrong, what changed, and why

After the fix, Kyrozen tracks the outcome. If you say "thanks, that works" it records a success. If you say "still broken" it records a failure and triggers deeper analysis.

### Git operations (safety-first)

- Always runs `git_status` first
- Reviews `git_diff` before committing
- Uses conventional commit prefixes: `fix:`, `feat:`, `refactor:`, `chore:`
- Never force-pushes without explicit request
- Stashes uncommitted changes before switching branches
- Warns before `git reset --hard`

### Complex tasks (never stops early)

For multi-step work (refactors, project generators, codebase audits):

- Breaks down the request into verifiable subtasks
- Creates a numbered Plan
- Builds a JSON TaskList mapped to each plan step
- Tracks progress with `TaskDone` markers
- Auto-generates a summary when complete

---

## 🧬 Profile-scoped self-evolution

OpenKyrozen learns reusable policies and skills only from completed multi-step work, explicit corrections, or verified failures. Learned artifacts are isolated to either the `coder` or `researcher` profile; the `reviewer` can propose one bounded canary during idle review but never learns its own behavior. The background loop waits for at least 60 seconds without user interaction (and each review candidate must be at least 30 seconds old).

### How it works

Every run records its profile, task signature, tool/error receipts, acceptance evidence, latency, and tokens in SQLite. Matching is deterministic and injects at most three artifacts (8,000 characters total), including at most one canary. A canary promotes only after two distinct verified successes and a non-regressing paired shadow replay; one linked correction, or two verified failures among its last five active uses, rolls it back to its predecessor. Learned artifacts cannot add permissions or dynamic tools, and user/bundled/plugin skills are immutable to evolution.

Use `/agent auto|coder|researcher` to control routing. `/learning status [profile]`, `/learning metrics [profile]`, `/learning evidence <id>`, `/learning explain <id>`, `/learning replay <id>`, and `/learning rollback <id>` expose lifecycle state and proof. The terminal replay command is informational; paired frozen results are submitted through the authenticated API, which never executes replay commands. Project indexing remains separate knowledge ingestion. See the [self-evolution and memory operations guide](docs/self-evolution.md) for verified workflows and request examples.

Inferred facts and preferences remain candidates until repeated independent evidence supports them. Explicit owner claims activate immediately. Typed global, profile, project, task, speaker, audience, and channel scopes resolve deterministically; `/memory why <id>` explains a claim and `/memory forget <id>` removes it together with solely dependent learned behavior.

Selection ranks relevant guidance by verified utility per context character and avoids artifact pairs with repeated verified failures. Paired omission trials can retire guidance only when removing it does not reduce verified completion; retirement is reversible and preserves the learned artifact pre-image.

Learned guidance is bound to the provider/model family that produced its evidence unless paired replay validates it across models. Verifier reliability, evidence-adaptive review priority, and a user-owned `KYROZEN_LEARNING_CONSTITUTION` file constrain evolution. Redacted experience capsules are portable JSON evidence, but imports always remain inactive candidates until local validation.

Multi-party claims distinguish attributed beliefs, private facts, and group agreements. Speaker, audience, channel, and visibility checks run before recall; private claims require the authenticated request actor, not a client-supplied speaker label. Chat responses include a memory receipt listing the claim IDs and speakers that affected the turn, and `benchmarks/multi_party_memory.jsonl` provides frozen leakage, update, ambiguity, and audience cases.

### Memory storage

OpenKyrozen v2 uses **SQLite as the source of truth** (`~/.kyrozen/v2/openkyrozen.sqlite3`) and ChromaDB as a rebuildable semantic index. Memories have a kind, scope, confidence, source events, and lifecycle status. Workspaces and sessions are isolated, raw observations are marked as data, and `/forget` removes records by durable ID. If ChromaDB is unavailable, SQLite keeps durable keyword retrieval.

Import an existing v1 store without deleting it:

```bash
python main.py migrate v1 ./chroma_memory
```

The migration creates a `.v1-backup` copy and writes the v2 database under `~/.kyrozen/v2/` (or `KYROZEN_DB_PATH`).

### v2 durable tasks and learning

Tasks persist across process restarts and use `pending`, `running`, `succeeded`, `failed`, `blocked`, and `cancelled` states. `TaskDone` is only a completion request; a successful tool result, test, file check, or explicit confirmation must provide evidence before a task can succeed. API-created tasks with an explicit safe `action` and string `args` are picked up by the durable worker after server restart; failed or blocked tasks require an explicit resume request.

```bash
curl -X POST http://127.0.0.1:8000/api/v2/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description":"write a marker","action":"write_file","args":"task-marker.txt|completed","acceptance":["marker written"]}'
# Resume a failed/blocked task:
curl -X POST http://127.0.0.1:8000/api/v2/tasks/<task-id>/resume
```

Run a frozen clean-versus-evolved benchmark with identical case order and runner settings:

```bash
python main.py learning benchmark --cases cases.jsonl \
  --clean-runner './clean-wrapper' --evolved-runner './evolved-wrapper' \
  --output benchmark.json
```

Each JSONL case requires `id`, `profile`, and `task`. Each wrapper reads one case from stdin and emits `verified_success`, `corrections`, `repeated_errors`, `tool_calls`, `tokens`, and `latency`. The exported `openkyrozen-learning-benchmark-v1` JSON is harness-neutral and suppresses a superiority claim unless completion is non-regressing, statistically credible, and a secondary measure improves.

---

## 🌐 Web UI & REST API

```bash
pip install fastapi uvicorn
python server.py --port 8000
# Open http://localhost:8000

# For LAN or container access, set a token and bind explicitly:
KYROZEN_SERVER_TOKEN=change-me python server.py --host 0.0.0.0 --port 8000
```

### REST API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dark-themed chat web UI |
| `POST` | `/api/chat` | Send a message with optional `profile`, `speaker`, `audience`, and `channel`; returns a memory receipt |
| `POST` | `/api/chat/stream` | SSE streaming chat with the same optional profile and memory context |
| `GET` | `/api/memory?q=keyword` | Search stored memories |
| `GET` | `/api/v2/memory?q=keyword&speaker=...&audience=...&channel=...` | Structured memory search with provenance and party scope |
| `GET/POST` | `/api/v2/tasks` | Durable task listing and creation |
| `GET` | `/api/v2/learning` | Learning proposal status |
| `GET` | `/api/v2/learning/metrics?profile=...` | Profile completion, correction, error, tool, token, and latency metrics |
| `GET` | `/api/v2/learning/{id}/evidence` | Proof card, applicability, replay, and outcome receipts |
| `POST` | `/api/v2/learning/{id}/replay` | Record paired sandboxed candidate/predecessor replay results |
| `POST` | `/api/v2/learning/{id}/omission` | Record paired with/without-artifact results |
| `POST` | `/api/v2/learning/{id}/retire` | Retire an artifact with non-regressing omission evidence |
| `POST` | `/api/v2/learning/{id}/restore` | Restore a retired artifact as a canary |
| `GET` | `/api/v2/learning/{id}/capsule` | Export a redacted, harness-neutral experience capsule |
| `POST` | `/api/v2/learning/capsules` | Import a capsule as an inactive candidate |
| `GET` | `/api/v2/learning/constitution` | Inspect the immutable user-owned learning policy |
| `POST` | `/api/v2/learning/{id}/rollback` | Roll back an activated proposal |
| `GET/POST` | `/api/v2/memory/claims` | List or create typed, attributed memory claims; filter with `speaker`, `audience`, and `channel` |
| `GET/DELETE` | `/api/v2/memory/claims/{id}` | Explain or dependency-completely forget a claim with party filters |
| `GET` | `/api/v2/events` | Auditable runtime, task, session, and learning events |
| `GET/POST` | `/api/v2/schedules` | Durable interval and one-shot Gateway jobs |
| `POST` | `/api/v2/schedules/{id}/disable` | Disable a scheduled job |
| `GET` | `/api/v2/skills` | List installed candidate/active skills |
| `POST` | `/api/v2/skills/install` | Install and validate a local `SKILL.md` package |
| `POST` | `/api/v2/skills/{id}/activate` | Activate a validated skill |
| `POST` | `/api/v2/skills/{id}/rollback` | Roll back a skill |
| `GET` | `/api/v2/sessions` | List durable sessions |
| `GET` | `/api/v2/sessions/{id}` | Resume/read a session context |
| `GET` | `/api/v2/agents` | List specialised sub-agent profiles |
| `POST` | `/api/v2/agents/run` | Run a sub-agent with isolated memory and capabilities |

Browser tools (`browser_open`, `browser_snapshot`, `browser_click`, `browser_type`, and
`browser_close`) use an isolated profile and are available after
`pip install 'openkyrozen[browser]' && playwright install chromium`. Private and
loopback destinations are blocked unless `KYROZEN_BROWSER_ALLOW_PRIVATE=1` is set.
| `GET` | `/api/cost` | Token usage and cost summary |
| `GET` | `/api/health` | Provider status + memory count |
| `GET` | `/api/voice/speak?text=...` | Text-to-speech via system TTS |
| `POST` | `/api/voice/transcribe` | Speech-to-text (passthrough) |
| `POST` | `/api/webhooks/register` | Register a webhook URL |
| `GET` | `/api/webhooks` | List registered webhooks |
| `POST` | `/api/webhooks/test` | Fire a test webhook |
| `POST` | `/mcp` | Model Context Protocol (JSON-RPC 2.0) |

API and MCP routes allow direct loopback access without a token. Any
non-loopback deployment must set `KYROZEN_SERVER_TOKEN` and send it as
`Authorization: Bearer <token>` or `X-Kyrozen-Token`. Web/MCP use the rich
`workspace` profile by default; choose `full` explicitly for irreversible reset
and dynamic tools. Authentication, capability tokens, and command safety checks
still apply.

The Web/MCP server is intentionally a **single-user deployment**. One
`KYROZEN_SERVER_TOKEN` represents the one owner of that server's private
memories, tasks, events, schedules, and learning state; request JSON cannot
choose another private speaker. Loopback and token-authenticated requests use
the same stable actor. Set `KYROZEN_SERVER_ACTOR` to a stable, non-secret label
when migrating an existing deployment; otherwise the default actor is `local`.
Public and group-attributed claims may name other speakers, but private claims
must belong to the deployment actor. Use separate deployments and databases
for separate private users.

### Docker deployment

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -v kyrozen-data:/root/.kyrozen/v2 \
  openkyrozen
```

---

## 🔌 Plugin System

Create a `.py` file in `plugins/` with a `register()` function:

```python
# plugins/my_plugin.py
class MyPlugin:
    def on_startup(self, agent=None, **kwargs):
        print("Plugin loaded!")

    def on_turn_start(self, user_input, **kwargs):
        print(f"User said: {user_input[:50]}")

    def on_tool_execute(self, action, args, result, **kwargs):
        print(f"Tool {action}({args[:30]}) → {result[:30]}")

def register():
    return MyPlugin()
```

Available hooks: `on_startup`, `on_turn_start`, `on_turn_end`, `on_tool_execute`.

See `plugins/turn_logger.py` for a working example.

---

## 🔐 Security

| Feature | What it protects |
|---------|-----------------|
| **Dangerous command filter** | Blocks `rm -rf`, `mkfs`, fork bombs, Windows destructive commands |
| **API key encryption** | Fernet encryption with a random per-install secret; config and secret files use `0600` permissions |
| **Prompt injection protection** | Filters known patterns in CLI, API, and MCP messages |
| **Workspace boundary** | File and directory tools reject paths outside the active workspace |
| **API authentication** | Non-loopback API/MCP access requires `KYROZEN_SERVER_TOKEN` |
| **Capability profiles** | Local CLI keeps the full agent toolset; Web/MCP default to rich `workspace` access, while `full` explicitly enables irreversible Git reset and dynamic tools |
| **Git safety** | Never force-pushes; CLI confirms high-impact Git actions and records the decision |
| **Audit logging** | All chat/API events logged to `kyrozen_audit.log` with timestamps |
| **Python version guard** | Refuses to start on Python 3.14+ |
| **Tool failure memory** | Remembers past failures and avoids repeating them |

---

## ⚙️ Configuration Reference

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `KYROZEN_PROVIDER` | LLM provider | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `GEMINI_API_KEY` | Google Gemini API key | — |
| `KYROZEN_API_KEY` | Universal API key (overrides provider-specific) | — |
| `KYROZEN_MODEL_SIMPLE` | Model for simple/medium tasks | Provider default |
| `KYROZEN_MODEL_COMPLEX` | Model for complex tasks | Provider default |
| `KYROZEN_BASE_URL` | Custom API base URL | Provider default |
| `KYROZEN_EXECUTION_SURFACE` | Execution surface (`cli` or `web`) | `cli` |
| `KYROZEN_ALLOW_DYNAMIC_TOOLS` | Allow LLM-generated Python tools (`1`/`true`) | CLI: enabled; Web/MCP: disabled |
| `KYROZEN_APPROVAL_MODE` | CLI confirmation mode for high-impact Git actions (`dangerous`/`never`) | `dangerous` |
| `KYROZEN_WEB_CAPABILITIES` | Web chat capabilities: `readonly`, `workspace`, or `full` | `workspace` |
| `KYROZEN_MCP_CAPABILITIES` | MCP capabilities: `readonly`, `workspace`, or `full` | `workspace` |

The local CLI is intentionally a high-permission agent, similar to Codex or OpenClaw: it can read and write the active workspace, run shell commands, use the network, and operate Git. The Web and MCP surfaces expose the same rich `workspace` profile by default, but keep irreversible `git_reset` and LLM-generated Python tools behind the explicit `full`/`KYROZEN_ALLOW_DYNAMIC_TOOLS=1` opt-in. Authentication and the command safety filter still apply.

### Config file (`~/.kyrozen_config.json`)

```json
{
  "provider": "deepseek",
  "api_key": "<encrypted>",
  "model_simple": "deepseek-chat",
  "model_complex": "deepseek-reasoner",
  "encrypted": true,
  "encryption": "fernet"
}
```

The file is auto-managed. Use `/provider` or `/api_key` in-chat to update it interactively.

---

## 🔧 Development

```bash
# Quick verification
make check

# Syntax lint only
make lint

# Debug mode (format-error traps)
make debug

# First-time API key setup
make init

# Rebuild venv after Python version change
make reinstall

# Launch web server
make web

# Git helpers
make git-status
make git-log
make commit msg='feat: description'
make push
```

### CI/CD

GitHub Actions automatically runs on every push and PR:
- Syntax check across Python 3.12 and 3.13
- Tool inventory validation
- Provider import check
- Docker build verification

### pip package

```bash
# Install from local directory (PyPI publishing coming soon)
pip install .                   # core + CLI
pip install .[web]              # + web UI
pip install .[all]              # + Claude + Gemini + web
```

---

## 📁 Project structure

```
OpenKyrozen/
├── main.py              # Core agent loop, self-learning, chat turn logic
├── tools.py             # 29 base tools; main.py adds two memory actions
├── providers.py         # Multi-LLM abstraction (5 providers + fallback)
├── memory.py            # SQLite memory with optional rebuildable Chroma index
├── server.py            # FastAPI web server + REST API + chat UI
├── pyproject.toml       # pip package configuration
├── Dockerfile           # Docker image definition
├── Makefile             # Build automation (macOS/Linux)
├── setup.bat / run.bat  # Windows batch scripts
├── plugins/             # Plugin directory (hook-based)
├── prompts/             # Prompt templates (role, instructions, examples)
├── chroma_memory/       # ChromaDB persistent storage (auto-created)
└── .github/workflows/   # CI/CD pipeline
```

---

## 🙏 Standing on the Shoulders of Giants

OpenKyrozen is built on top of incredible open-source work. We're grateful to every maintainer and contributor who made these projects possible.

| Project | Repo | What we use it for |
|---------|------|-------------------|
| **Aider** | [paul-gauthier/aider](https://github.com/paul-gauthier/aider) | Inspired our multi-turn agent loop, tool-calling patterns, and git-safety conventions |
| **CodeWhale** | [deepseek-ai/codewhale](https://github.com/deepseek-ai/codewhale) | Agent runtime architecture, sub-agent delegation, and verification discipline |
| **Chroma** | [chroma-core/chroma](https://github.com/chroma-core/chroma) | Vector database powering our long-term memory and semantic recall |
| **FastAPI** | [fastapi/fastapi](https://github.com/fastapi/fastapi) | Web server, REST API, and real-time streaming endpoints |
| **Rich** | [Textualize/rich](https://github.com/Textualize/rich) | Terminal UI — panels, progress bars, syntax highlighting, and live displays |
| **OpenAI Python** | [openai/openai-python](https://github.com/openai/openai-python) | Unified API client for DeepSeek, OpenAI, and Ollama providers |
| **Uvicorn** | [encode/uvicorn](https://github.com/encode/uvicorn) | ASGI server for production web deployments |
| **googlesearch-python** | [Nv7-GitHub/googlesearch](https://github.com/Nv7-GitHub/googlesearch) | Web search fallback when DuckDuckGo is unavailable |

> *"If I have seen further, it is by standing on the shoulders of giants."* — Isaac Newton

---

## 📄 License

MIT License. See `LICENSE` for details.

---

<p align="center">
  <sub>Built with ❤️ for developers who want an AI that learns</sub>
</p>
