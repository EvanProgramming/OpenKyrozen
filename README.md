<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek%20%7C%20OpenAI%20%7C%20Claude%20%7C%20Gemini%20%7C%20Ollama-API-green?logo=openai" alt="Multi-Provider">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
  <img src="https://img.shields.io/badge/CI-passing-brightgreen" alt="CI">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>Self‑learning AI Agent — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>
<p align="center">A terminal-native, fully autonomous AI agent that <em>learns from every interaction</em>,<br>operates your filesystem, manages git, fixes bugs, and improves itself over time.</p>

---

## What is OpenKyrozen?

OpenKyrozen is a **self-learning AI agent** that runs in your terminal. Unlike a typical chatbot, it:

- **Uses 24 built-in tools** — read/write files, execute shell commands, search the web, manage git repositories
- **Learns continuously** — 20 self-learning features run in the background, extracting facts, inventing skills, and improving strategies
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

### Option A: pip install (recommended)

```bash
pip install openkyrozen

# Launch the terminal agent
kyrozen

# Or launch the web server
kyrozen-web
```

### Option B: From source

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
| `/self-learning` | Toggle individual self-learning features on/off |

### Web UI mode

```bash
python server.py --port 8000
# Open http://localhost:8000

# Or via Docker:
docker build -t openkyrozen .
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... openkyrozen
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
│  Tool Executor   │──► 24 built-in tools (file I/O, shell, git, web, memory)
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

All 24 tools accept a plain-string `args` field in a JSON action block:

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

## 🧬 Self-Learning System

This is what makes Kyrozen different. **20 self-learning features** run continuously in the background — no manual saving needed. The agent gets smarter the longer you use it.

### How it works

Every 30 seconds (when you're idle), Kyrozen runs a learning cycle. Each feature can be toggled on/off with `/self-learning`.

| # | Feature | What it learns |
|---|---------|---------------|
| 1 | **Conversation learning** | Extracts facts, preferences, and patterns from chat |
| 2 | **Project file scanning** | Reads every `.py` file into memory for context |
| 3 | **Stale entry aging** | Removes facts about files that no longer exist |
| 4 | **Tool auto-debug** | Analyzes tool failures, finds root causes |
| 5 | **Memory consolidation** | Deduplicates and summarizes stored facts |
| 6 | **Tool review** | Suggests under-used tools for removal |
| 7 | **Targeted inquiry** | Finds undocumented functions and infers their purpose |
| 8 | **Idle reflection** | After complex tasks, reflects on what went well |
| 9 | **Strategy distillation** | When token usage is high, distills efficiency tips |
| 10 | **New tech auto-patch** | Queries the web when you mention unknown libraries |
| 11 | **Skill invention** | Creates reusable skill templates from past successes |
| 12 | **Context compression** | Summarizes old turns when context exceeds 30K chars |
| 13 | **Fix verification** | Tracks bug-fix success rate over time |
| 14 | **Dynamic tool creation** | `DefineTool:` syntax lets the agent build new tools |
| 15 | **User preference model** | Detects your coding style, preferred language, verbosity |
| 16 | **Autonomous inspection** | Checks for outdated packages, code smells, gitignore gaps |
| 17 | **Memory importance scoring** | Rates entries 0-10; high-score entries get priority |
| 18 | **Knowledge graph** | Builds entity→relationship maps from stored facts |
| 19 | **Skill composition** | Chains multiple learned skills into workflows |
| 20 | **Bad learning rollback** | `/forget` command to remove incorrect learnings |

### Memory storage

Long-term memory uses **ChromaDB** (vector database, stored in `chroma_memory/`). Falls back to in-memory storage if ChromaDB is unavailable. Memories are semantically searchable — the agent can recall relevant facts from weeks ago.

---

## 🌐 Web UI & REST API

```bash
pip install fastapi uvicorn
python server.py --port 8000
# Open http://localhost:8000
```

### REST API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dark-themed chat web UI |
| `POST` | `/api/chat` | Send a message, get JSON response |
| `POST` | `/api/chat/stream` | SSE streaming chat |
| `GET` | `/api/memory?q=keyword` | Search stored memories |
| `GET` | `/api/cost` | Token usage and cost summary |
| `GET` | `/api/health` | Provider status + memory count |
| `POST` | `/api/voice/speak?text=...` | Text-to-speech via system TTS |
| `POST` | `/api/voice/transcribe` | Speech-to-text (passthrough) |
| `POST` | `/api/webhooks/register` | Register a webhook URL |
| `GET` | `/api/webhooks` | List registered webhooks |
| `POST` | `/api/webhooks/test` | Fire a test webhook |
| `POST` | `/mcp` | Model Context Protocol (JSON-RPC 2.0) |

### Docker deployment

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -v $(pwd)/chroma_memory:/app/chroma_memory \
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
| **API key encryption** | `~/.kyrozen_config.json` encrypted at rest (XOR + machine-derived SHA-256 key) |
| **Prompt injection protection** | Detects 9 common injection patterns and filters them |
| **Sandbox execution** | File operations restricted to workspace boundary |
| **Git safety** | Never force-pushes, warns before hard resets |
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

### Config file (`~/.kyrozen_config.json`)

```json
{
  "provider": "deepseek",
  "api_key": "<encrypted>",
  "model_simple": "deepseek-chat",
  "model_complex": "deepseek-reasoner",
  "encrypted": true
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
pip install openkyrozen         # core + CLI
pip install openkyrozen[web]    # + web UI
pip install openkyrozen[all]    # + Claude + Gemini + web
```

---

## 📁 Project structure

```
OpenKyrozen/
├── main.py              # Core agent loop, self-learning, chat turn logic
├── tools.py             # 24 built-in tools (file, shell, git, web)
├── providers.py         # Multi-LLM abstraction (5 providers + fallback)
├── memory.py            # ChromaDB-backed vector memory
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

## 📄 License

MIT License. See `LICENSE` for details.

---

<p align="center">
  <sub>Built with ❤️ for developers who want an AI that learns</sub>
</p>
