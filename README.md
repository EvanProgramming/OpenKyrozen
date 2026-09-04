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
  - [Browser](#browser-5-tools)
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
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-... \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -v kyrozen-data:/data \
  openkyrozen
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

Ollama is keyless: set `KYROZEN_PROVIDER=ollama` and, if needed, point
`KYROZEN_BASE_URL` at the local OpenAI-compatible endpoint. The Web server
always initialises in headless mode. If a remote provider has no key, it
starts in a documented degraded state without reading stdin; configure the
provider before sending chat requests. The interactive CLI still prompts for
providers that require credentials.

## 🧬 Self-Learning System

OpenKyrozen has one authoritative registry of 20 independently toggleable
learning features. Each dispatcher cycle runs a bounded round-robin subset and
stores `learning.feature_started`, `learning.feature_completed`, or
`learning.feature_failed` in the scoped SQLite event store. A completion records
whether it changed durable or in-memory state; `changed: false` is an honest
no-op when the required evidence or input is absent.

The CLI dispatches up to four features every 30 seconds while idle. Web and
Gateway processes use the durable `learning_cycle` scheduler job. Chat turns
also dispatch the input-dependent preference and technology features. The
same dispatcher and feature flags are used by all surfaces. Inspect the
latest status with `GET /api/v2/learning/features` or toggle flags with
`/self-learning`. Dynamic tools remain response-time operations protected by
explicit capability and approval gates, and rollback remains user-directed via
`/forget` or an explicit learning rollback command.

### Installed skill guidance

The local skill API installs user-authored `SKILL.md` packages as guidance that
can participate in matching runtime tasks. A package intended for matching must
include a `skill.json` with explicit supported profiles and one to twelve
trigger terms:

```json
{
  "name": "local-pytest-guide",
  "version": "1.0.0",
  "permissions": [],
  "profiles": ["coder"],
  "triggers": ["pytest"]
}
```

Install it with `POST /api/v2/skills/install` and
`{ "path": "...", "activate": true }`. Only an active local skill whose
profile and trigger match the task is added to the prompt; each use creates a
scoped `learning.artifact_used` receipt. Skill text is untrusted procedure
data: it cannot grant capabilities, permissions, or approval, and the normal
tool and workspace gates still apply. Learned artifacts remain separately
outcome-verified and follow their canary/promotion lifecycle; plugins remain
hook extensions rather than permission-bearing skill packages.

| # | Registry feature | Bounded effect |
|---:|---|---|
| 1 | Conversation learning | Extract candidate facts from new conversation logs |
| 2 | Project-file loading | Incrementally update scoped Python-file snapshots |
| 3 | Code-entry aging | Remove snapshots for deleted Python files |
| 4 | Tool auto-debugging | Record findings from repeated tool failures |
| 5 | Memory consolidation | Deduplicate and summarize non-trivial memories |
| 6 | Tool review | Record bounded tool-improvement suggestions |
| 7 | Targeted inquiry | Inspect one undocumented function per cycle |
| 8 | Idle reflection | Reflect only after the configured idle interval |
| 9 | Strategy distillation | Distill strategies after sufficient recent usage |
| 10 | Technology discovery | Queue bounded documentation fetches for new libraries |
| 11 | Skill invention | Create candidate reusable workflows from repeated work |
| 12 | Context compression | Summarize old turns after the context threshold |
| 13 | Outcome-verified evolution | Review one eligible trajectory and canary |
| 14 | Dynamic-tool definition | Observe the inventory; never grant capability automatically |
| 15 | Preference detection | Persist newly detected user preference signals |
| 16 | Autonomous inspection | Run one bounded project health inspection |
| 17 | Memory importance scoring | Score a recent window and persist the scores |
| 18 | Knowledge-graph extraction | Extract bounded entity relationships from facts |
| 19 | Skill composition | Record a matching learned workflow without executing it |
| 20 | Learning rollback | Keep automatic deletion disabled; require explicit user action |

---

## 🛠 Tools Reference

All 31 runtime tools accept a plain-string `args` field in a JSON action block.
The [generated runtime inventory](docs/tool-inventory.md) is authoritative for
tool names, capability labels, MCP input schemas, and the live HTTP endpoint
list:

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
| `execute_terminal_command` | Alias for `run_cmd` | `"python --version"` |

File and directory paths are resolved inside the active workspace. Use a
relative path such as `notes.txt` in file examples; home-directory and Desktop
paths are rejected by the workspace boundary.

`run_cmd` prepends the `bin` directory of the Python interpreter running
OpenKyrozen, so bare `python` and `pip` resolve inside the active virtual
environment even when the parent shell was not activated. Explicit interpreter
paths such as `/usr/bin/python3` are left unchanged.

### Web

| Tool | Description | Example |
|------|-------------|---------|
| `search_web` | Internet search (Google → DDG → Wikipedia) | `"latest Python release"` |
| `read_webpage` | Fetch URL text content | `"https://example.com"` |
| `analyze_remote_repo` | Clone and summarize a remote repository | `"https://github.com/org/repo"` |

### Browser (5 tools)

These tools use an isolated browser profile. Install the optional browser
extra before using them.

| Tool | Description | Example |
|------|-------------|---------|
| `browser_open` | Open a URL | `"https://example.com"` |
| `browser_snapshot` | Read the current page text | `"session-id"` |
| `browser_click` | Click a CSS selector | `"session-id\|button.submit"` |
| `browser_type` | Fill a CSS selector | `"session-id\|input[name=q]\|query"` |
| `browser_close` | Close an isolated browser session | `"session-id"` |

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

The runtime persists this workflow per user/workspace/session as
`reported → reproduced → diagnosed → hypothesized → fixed → verified → explained`.
Reproduction, mutation, and verification stages each require bounded tool
receipts; a response that says the bug is fixed without a successful fix and a
successful verification command is blocked and cannot be presented as a
success. A failed verification is also recorded as `blocked`. Every turn and
feedback signal is linked to the workflow's task and attempt IDs in SQLite.
If you say "thanks, that works" it records positive feedback for that attempt;
if you say "still broken" it records negative feedback and blocks the attempt
for a new diagnosed run.

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

For model-generated complex work, give each `TaskList` item a stable `id` when the same turn can be retried; that ID is preferred over the description and is scoped to the authenticated user, workspace, and session. A `TaskList` update never clears recovered progress. `done` remains readable for old records but new progress and completion summaries use `succeeded`. `Plan`, `TaskList`, `TaskDone`, `Thought`, and `Action` are control blocks: the chat surface uses them internally and returns the latest natural-language response (or a deterministic tool/evidence summary), never a stale `Action` block.

```bash
curl -X POST http://127.0.0.1:8000/api/v2/tasks \
  -H 'Content-Type: application/json' \
  -d '{"description":"write a marker","action":"write_file","args":"task-marker.txt|completed","acceptance":["marker written"]}'
# Resume a failed/blocked task:
curl -X POST http://127.0.0.1:8000/api/v2/tasks/<task-id>/resume
```

Run the frozen clean-versus-evolved benchmark from a fresh checkout after
`make install`:

```bash
make benchmark
```

`make benchmark` uses the repository's
`benchmarks/multi_party_memory.jsonl` fixture and the shipped
`benchmarks/clean_runner.py` and `benchmarks/evolved_runner.py` wrappers. It
creates a temporary benchmark root, assigns separate `clean.sqlite3` and
`evolved.sqlite3` databases, and isolates the benchmark driver's database with
`KYROZEN_DB_PATH`; the temporary root is removed when the command exits. The
wrappers use a deterministic local memory policy, so no provider, model, or
API key is required. Set `BENCHMARK_TIMEOUT=120` to change the per-case limit.

For direct use, provide an isolated root explicitly:

```bash
benchmark_root="$(mktemp -d)"
KYROZEN_BENCHMARK_ROOT="$benchmark_root" \
  ./venv/bin/python main.py learning benchmark \
  --cases benchmarks/multi_party_memory.jsonl \
  --clean-runner "./venv/bin/python benchmarks/clean_runner.py" \
  --evolved-runner "./venv/bin/python benchmarks/evolved_runner.py"
rm -rf "$benchmark_root"
```

Each JSONL case requires `id`, `profile`, and `task`. Each wrapper reads one
case from stdin and emits `verified_success`, `corrections`,
`repeated_errors`, `tool_calls`, `tokens`, `latency`, `provider`, `model`, and
`evidence_status`, plus bounded evidence counts and scope. The exported
`openkyrozen-learning-benchmark-v1` JSON includes the protocol, runner
metadata, provider/model, and evidence policy. Fixture-only evidence is
reported as `fixture_verified` and therefore cannot support a product
superiority claim; such a claim requires independently verified paired runs
with the same provider, model, configuration, and observable product behavior.

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
| `GET` | `/api/cost` | Token usage and cost summary |
| `GET` | `/api/health` | Provider status + memory count |
| `GET` | `/api/memory?q=keyword` | Search stored memories |
| `GET` | `/api/v2/agents` | List specialised sub-agent profiles |
| `POST` | `/api/v2/agents/run` | Run a sub-agent with isolated memory and capabilities |
| `GET` | `/api/v2/events` | Auditable runtime, task, session, and learning events |
| `GET` | `/api/v2/learning` | Learning proposal status |
| `POST` | `/api/v2/learning/capsules` | Import a capsule as an inactive candidate |
| `GET` | `/api/v2/learning/constitution` | Inspect the immutable user-owned learning policy |
| `GET` | `/api/v2/learning/features` | Authoritative 20-feature registry and latest run status |
| `GET` | `/api/v2/learning/metrics?profile=...` | Profile completion, correction, error, tool, token, and latency metrics |
| `GET` | `/api/v2/learning/{proposal_id}/capsule` | Export a redacted, harness-neutral experience capsule |
| `GET` | `/api/v2/learning/{proposal_id}/evidence` | Proof card, applicability, replay, and outcome receipts |
| `POST` | `/api/v2/learning/{proposal_id}/omission` | Record paired with/without-artifact results |
| `POST` | `/api/v2/learning/{proposal_id}/replay` | Record paired sandboxed candidate/predecessor replay results |
| `POST` | `/api/v2/learning/{proposal_id}/restore` | Restore a retired artifact as a canary |
| `POST` | `/api/v2/learning/{proposal_id}/retire` | Retire an artifact with non-regressing omission evidence |
| `POST` | `/api/v2/learning/{proposal_id}/rollback` | Roll back an activated proposal |
| `GET` | `/api/v2/memory?q=keyword&speaker=...&audience=...&channel=...` | Structured memory search with provenance and party scope |
| `GET` | `/api/v2/memory/claims` | List typed, attributed memory claims; filter with `speaker`, `audience`, and `channel` |
| `POST` | `/api/v2/memory/claims` | Create a typed, attributed memory claim |
| `DELETE` | `/api/v2/memory/claims/{claim_id}` | Dependency-completely forget a claim with party filters |
| `GET` | `/api/v2/memory/claims/{claim_id}` | Explain a claim with party filters |
| `GET` | `/api/v2/schedules` | List durable interval and one-shot Gateway jobs |
| `POST` | `/api/v2/schedules` | Create a durable interval or one-shot Gateway job |
| `POST` | `/api/v2/schedules/{job_id}/disable` | Disable a scheduled job |
| `GET` | `/api/v2/sessions` | List durable sessions |
| `GET` | `/api/v2/sessions/{session_id}` | Resume/read a session context |
| `GET` | `/api/v2/skills` | List installed candidate/active skills |
| `POST` | `/api/v2/skills/install` | Install and validate a local `SKILL.md` package |
| `POST` | `/api/v2/skills/{skill_id}/activate` | Activate a validated skill |
| `POST` | `/api/v2/skills/{skill_id}/rollback` | Roll back a skill |
| `GET` | `/api/v2/tasks` | List durable tasks |
| `POST` | `/api/v2/tasks` | Create a durable task |
| `POST` | `/api/v2/tasks/{task_id}/resume` | Explicitly resume a failed or blocked durable task |
| `GET` | `/api/voice/speak?text=...` | Text-to-speech via system TTS |
| `POST` | `/api/voice/transcribe` | Speech-to-text (passthrough) |
| `GET` | `/api/webhooks` | List registered webhooks |
| `POST` | `/api/webhooks/register` | Register a webhook URL |
| `POST` | `/api/webhooks/test` | Fire a test webhook |
| `POST` | `/mcp` | Model Context Protocol (JSON-RPC 2.0) |

Browser tools (`browser_open`, `browser_snapshot`, `browser_click`, `browser_type`, and
`browser_close`) use an isolated profile and are available after
`pip install 'openkyrozen[browser]' && playwright install chromium`. Private and
loopback destinations are blocked unless `KYROZEN_BROWSER_ALLOW_PRIVATE=1` is set.

Successful `POST /api/chat` requests emit one `chat.completed` webhook after
the reply is produced. `POST /api/chat/stream` emits the same event only after
the SSE stream has sent `[DONE]`; failed or disconnected streams do not emit
it. The POST body is `{"event":"chat.completed","data":{...}}`, where
`data` contains only the bounded `actor`, `session_id`, `profile`,
`reply_summary`, `reply_length`, and `streamed` fields. Reply summaries are
limited to 500 characters and redact common API-key/token patterns. Webhook
delivery errors are audited as `WEBHOOK_FAILURE` and never change the chat
response.

API and MCP routes allow direct loopback access without a token. Any
non-loopback deployment must set `KYROZEN_SERVER_TOKEN` and send it as
`Authorization: Bearer <token>` or `X-Kyrozen-Token`. Web/MCP use the rich
`workspace` profile by default; choose `full` explicitly for irreversible reset
and dynamic tools. Authentication, capability tokens, and command safety checks
still apply.

The MCP endpoint supports `initialize`, `notifications/initialized`, `ping`,
`server/discover`, `tools/list`, `tools/call`, and the legacy `chat/send`
method. Responses echo the JSON-RPC request `id`. `tools/list` returns an
`inputSchema` for every exposed tool. Tools retain their internal plain-string
contracts while MCP object arguments are mapped explicitly; for example:

```bash
curl -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
curl -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
curl -sS http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"README.md"}}}'
```

Protocol errors use JSON-RPC error objects. Tool failures use a successful
JSON-RPC response whose result contains `isError: true`; capability-denied
and unknown tools remain protocol errors. Write, shell, network, destructive,
and dynamic tools continue to require their configured MCP capabilities.

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
  -e KYROZEN_DB_PATH=/data/openkyrozen.sqlite3 \
  -v kyrozen-data:/data \
  openkyrozen
```

The image runs as the non-root `kyrozen` user. SQLite is the durable source of
truth at `/data/openkyrozen.sqlite3` (the image sets `KYROZEN_DB_PATH` to this
path), so keep the named volume mounted at `/data` when replacing containers.
To run the same replacement-and-recovery check locally, use `make docker-smoke`.

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

The shared runtime loads `plugins/*.py` once per execution surface (`cli` and
`web`) in deterministic filename order. Hook failures are isolated, recorded
as `plugin.hook_failed` events, and never fail the user turn. Hook arguments
are keyword arguments and are bounded/redacted before a plugin sees them:

| Hook | Required kwargs | Additional context |
|------|-----------------|--------------------|
| `on_startup` | — | `agent`, `surface`, `user_id`, `workspace_id` |
| `on_turn_start` | `user_input` | `surface`, `user_id`, `workspace_id`, `session_id`, `profile` |
| `on_turn_end` | `reply`, `success` | `error` plus the turn context |
| `on_tool_execute` | `action`, `args`, `result`, `success` | `error`, `surface`, and scope context |

`on_tool_execute` fires once for every attempted action, including unknown,
unauthorized, malformed, and approval-denied actions. `turn_logger.py` writes
to `kyrozen_turns.log`; set `KYROZEN_TURN_LOG` to choose another path for a
service or test.

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
| `KYROZEN_APPROVAL_MODE` | CLI confirmation mode for high-impact Git actions and dynamic-tool registration (`dangerous`/`never`) | `dangerous` |
| `KYROZEN_WEB_CAPABILITIES` | Web chat capabilities: `readonly`, `workspace`, or `full` | `workspace` |
| `KYROZEN_MCP_CAPABILITIES` | MCP capabilities: `readonly`, `workspace`, or `full` | `workspace` |
| `KYROZEN_AGENT_CONFIG` | Explicit path to a validated `agent.yaml` configuration | Workspace `agent.yaml`, then packaged default |
| `KYROZEN_ROLE` | Override the configured role name | `assistant` |
| `KYROZEN_ROLE_PROMPT` | Override the configured role system prompt | Packaged `prompts/role.md` |
| `KYROZEN_INSTRUCTIONS` | Override the configured runtime instructions | Packaged `prompts/instructions.md` |
| `KYROZEN_AGENT_CAPABILITIES` | Comma-separated capability/profile upper bound | `full` |
| `KYROZEN_EXAMPLES` | JSON list of `{\"user\": ..., \"assistant\": ...}` examples | Packaged `prompts/examples.md` |

The local CLI is intentionally a high-permission agent, similar to Codex or OpenClaw: it can read and write the active workspace, run shell commands, use the network, and operate Git. The Web and MCP surfaces expose the same rich `workspace` profile by default, but keep irreversible `git_reset` and LLM-generated Python tools behind the explicit `full`/`KYROZEN_ALLOW_DYNAMIC_TOOLS=1` opt-in. On the interactive CLI, dynamic registration also follows `KYROZEN_APPROVAL_MODE`; use `never` only for an explicitly automated deployment. Authentication and the command safety filter still apply.

### Agent role configuration (`agent.yaml`)

`agent.yaml` is a validated, packaged configuration for the role and prompt
templates. A workspace-level `agent.yaml` customizes that workspace; use
`KYROZEN_AGENT_CONFIG=/path/to/agent.yaml` for an explicit file. The effective
precedence is environment-variable overrides, explicit configuration path,
workspace configuration, packaged configuration, and finally the built-in
defaults. The `role`, `instructions`, and `examples` fields are loaded from
the packaged `prompts/` templates by default and are included in installed
wheels.

The schema accepts `version`, `provider`, `role`, `instructions`, `examples`,
and `capabilities`. Capabilities are an upper bound: the active surface's
capability profile, approval mode, and authentication gates still apply, and
configuration cannot grant permissions. Invalid YAML, unknown fields,
duplicate keys, unsupported capabilities, and malformed examples fail with a
deterministic configuration error. See the root `agent.yaml` for a complete
example.

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

When automatic fallback is used, `auto`, an empty model, and the primary
provider's configured simple/complex model names are mapped to the equivalent
simple/complex model configured for each fallback provider. An explicit model
name that is not recognised as one of the primary provider's names is passed
through unchanged; use that only when the fallback endpoint supports the same
name. The mapping is identical for streaming and non-streaming requests, and
an all-provider failure retains every provider error with the primary failure
as its cause.

The file is auto-managed. Use `/provider` or `/api_key` in-chat to update it interactively.

---

## 🔧 Development

```bash
# Quick verification
make check
make docs-check
make shell-check

# Syntax lint only
make lint

# Debug mode (isolated, non-interactive smoke test; never prompts for a key)
make debug

# Start the normal interactive CLI explicitly from the debug entry point
./venv/bin/python main_debug.py --interactive

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
- Generated tool inventory and documentation consistency validation
- Provider import check
- Docker build and replace-container recovery smoke test

### pip package

```bash
# Install from local directory (PyPI publishing coming soon)
pip install .                   # core + CLI
pip install '.[web]'            # + web UI
pip install '.[all]'            # + Claude + Gemini + web
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
├── agent_config.py      # Strict agent.yaml loader and capability bound
├── agent.yaml           # Validated role/provider/capability configuration
├── pyproject.toml       # pip package configuration
├── Dockerfile           # Docker image definition
├── Makefile             # Build automation (macOS/Linux)
├── setup.bat / run.bat  # Windows batch scripts
├── plugins/             # Plugin directory (hook-based)
├── prompts/             # Prompt templates (role, instructions, examples)
├── docs/tool-inventory.md # Generated runtime tool and endpoint inventory
├── scripts/              # Reproducible documentation and smoke checks
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
