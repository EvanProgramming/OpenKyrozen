<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek-API-green?logo=openai" alt="DeepSeek">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>Self‑learning AI Agent — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>

---

## 🚀 Quick start

### Prerequisites
- Python **3.12** or **3.13** (Python 3.14 has a known import issue)
- An API key from one of the supported providers:
  - **DeepSeek** — free at [platform.deepseek.com](https://platform.deepseek.com)
  - **OpenAI** — [platform.openai.com](https://platform.openai.com)
  - **Anthropic (Claude)** — [console.anthropic.com](https://console.anthropic.com)
  - **Google (Gemini)** — [aistudio.google.com](https://aistudio.google.com)
  - **Ollama** — no key needed (runs locally)

### 🍎 macOS / 🐧 Linux

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

**Manual install:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 🪟 Windows

```cmd
setup.bat
run.bat
```

**Manual install:**
```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On first launch you will be prompted for an API key — it auto‑detects your provider and saves the key to `~/.kyrozen_config.json`.

---

## 🧠 Architecture

OpenKyrozen runs as an interactive chat loop. Each user turn goes through a pipeline:

```
User input → Task Classifier → Model Selector → LLM → Tool Executor → Response
                  │                  │           │         │
            simple/medium/     LLM Provider      │    run_cmd, read_file,
              complex          (see table)       │    write_file, git_*, ...
                                                │
                                        Tool results fed back to LLM
                                        (up to 50 tool-call rounds per turn)
```

### Task complexity routing

| Level | Trigger | Behaviour |
|-------|---------|-----------|
| **Simple** | Greetings, factual Q&A, single action | Direct reply, no planning overhead |
| **Medium** | 2–3 tool calls, file ops, web search | Plan block → execute → summarise |
| **Complex** | 4+ tools, code generation, bug fixing, git merge | Plan → TaskList → execute with progress tracking, never stops early |

### Model auto‑selection

Kyrozen picks the best model per request, using different models for simple vs complex tasks.
The exact models depend on your provider:

| Provider  | Simple tasks | Complex tasks |
|-----------|-------------|---------------|
| DeepSeek  | `deepseek-chat` | `deepseek-reasoner` |
| OpenAI    | `gpt-4o` | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |
| Google    | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Ollama    | `llama3.2` | `llama3.2` |

Override via environment variables: `KYROZEN_MODEL_SIMPLE`, `KYROZEN_MODEL_COMPLEX`.

### Switching providers

Set `KYROZEN_PROVIDER` to one of: `deepseek`, `openai`, `anthropic`, `google`, `ollama`.

Or use the in‑chat command `/provider` to switch interactively. The first launch prompts for an API key — it detects which provider to use from your environment or `~/.kyrozen_config.json`.

---

## 🛠 Available tools (24 built‑in)

### File & system
| Tool | Description |
|------|-------------|
| `read_file` | Read file contents by path |
| `write_file` | Write content to a file (`path\|content` format) |
| `list_dir` | List directory contents |
| `list_tree` | Recursive directory tree view |
| `find_files` | Find files by glob pattern |
| `run_cmd` | Execute a shell command (with safety filter) |
| `execute_terminal_command` | Alias for `run_cmd` |

### Web
| Tool | Description |
|------|-------------|
| `search_web` | Search the internet (Google → DuckDuckGo → Wikipedia fallback) |
| `read_webpage` | Fetch and extract text from a URL |

### Git (14 tools)
| Tool | Description |
|------|-------------|
| `git_status` | Show working tree status |
| `git_diff` | Show unstaged, staged, or between-commit diffs |
| `git_log` | Commit history with `--oneline --decorate` |
| `git_branch` | List / create / delete branches |
| `git_add` | Stage files |
| `git_commit` | Commit staged changes with a message |
| `git_push` | Push to remote |
| `git_pull` | Pull from remote |
| `git_checkout` | Switch branches or restore files |
| `git_stash` | Stash / pop / list working changes |
| `git_reset` | Reset HEAD (`--soft` safe, `--hard` warns) |
| `git_show` | Inspect a commit with `--stat` |
| `git_remote` | List / add / remove remotes |
| `git_clone` | Clone a repository |
| `analyze_remote_repo` | Clone + read all files → summary |

### Memory
| Tool | Description |
|------|-------------|
| `search_memory` | Semantic search over stored knowledge (ChromaDB) |
| `check_stored_data` | Show memory statistics and recent facts |

All tools accept **plain‑string arguments** in a JSON action block:
```json
{"action": "read_file", "args": "README.md"}
```

Tool name aliases are supported — e.g. `bash`, `cmd`, `sh` all map to `run_cmd`; `status`, `diff`, `log` map to `git_status`, `git_diff`, `git_log`.

---

## 🚦 Dedicated workflows

### Bug‑fixing

When you paste an error or traceback, Kyrozen detects it and activates a structured protocol:

1. **Reproduce** — read the offending code, re‑run the failing command
2. **Diagnose** — parse the traceback, find the root cause (does not guess)
3. **Hypothesise** — state the fix before editing
4. **Fix** — apply the minimal code change
5. **Verify** — re‑run the original command; if it still fails, loop to step 2
6. **Explain** — tell you what was wrong, what changed, and why

### Git operations

When you ask for git work, the agent follows a safety‑first protocol:

- Always runs `git_status` first to understand state
- Reviews changes with `git_diff` before committing
- Uses conventional commit prefixes (`fix:`, `feat:`, `refactor:`, `chore:`)
- Never force‑pushes without explicit request
- Stashes uncommitted changes before switching branches
- Warns before `git reset --hard`

### Complex tasks

For multi‑step work (codebase analysis, large refactors, project generators):

- Understands the full request and identifies subtasks
- Creates a numbered Plan with verifiable steps
- Builds a JSON TaskList mapped to each plan step
- Tracks progress with `TaskDone` markers
- Never skips tasks or stops early

---

## ⌨️ In‑chat commands

| Command | Description |
|---------|-------------|
| `/quit` `/exit` | Exit the agent |
| `/learn` | Immediately scan project files into memory |
| `/api_key` | Change your API key for the current provider |
| `/provider` | Switch to a different LLM provider (interactive menu) |
| `/forget` | Show or delete recent learnings (`/forget keyword` to delete) |
| `/update` | Refresh the agent from git |
| `/self-learning` | Interactive menu to toggle individual learning features |

---

## 🧬 Self‑learning system

OpenKyrozen learns continuously in the background — no manual saving needed. Every 30 seconds (when idle) it runs a learning cycle. All features can be individually toggled via `/self-learning`.

| Feature | What it does |
|---------|-------------|
| **Auto‑learn from conversations** | Extracts facts, preferences, and patterns from your chat history |
| **Scan project files** | Reads every `.py` file into memory for context |
| **Age out stale entries** | Removes or marks obsolete facts when source files change |
| **Auto‑debug tools** | Analyses tool failures and logs root‑cause diagnostics |
| **Consolidate memories** | Deduplicates and summarises stored facts |
| **Review & merge tools** | Suggests under‑used tools for removal or merging |
| **Targeted inquiry** | Finds undocumented functions in your codebase and infers their purpose |
| **Idle reflection** | After complex tasks, reflects on what went well or wrong |
| **Strategy distillation** | When token usage is high, distills efficiency tips |
| **Auto‑patch new tech** | Queries the web for new libraries mentioned in code |
| **Invent skills** | Creates reusable skill definitions from past successful patterns |
| **Context compression** | Summarises old conversation turns when context grows beyond 30K chars |
| **Fix verification** | Tracks bug‑fix outcomes (did it work?) and builds a success‑rate database |
| **Dynamic tool creation** | `DefineTool` syntax lets the agent create new callable tools from SKILLs |
| **User preference model** | Detects and remembers coding style, language, verbosity preferences |
| **Autonomous inspection** | Proactively checks for outdated packages, code smells, gitignore gaps |
| **Memory importance scoring** | Scores each memory entry 0-10; high-score entries get retrieval priority |
| **Knowledge graph extraction** | Builds structured entity-relationship map from stored facts |
| **Skill composition** | Chains multiple learned SKILLs into multi-step workflows |
| **Bad learning rollback** | `/forget` command to view and delete incorrect learnings |

### Memory storage

Long‑term memory uses **ChromaDB** (vector database) stored in `chroma_memory/` for semantic search. Falls back to in‑memory storage if ChromaDB is unavailable.

---

## 🔐 Safety

- **Dangerous command filter** — blocks `rm -rf`, `mkfs`, fork bombs, pipe‑to‑shell exploits, and Windows equivalents (`del /f /s C:\`, `format C:`, `diskpart`, `taskkill` on system processes, `reg delete HKLM`, etc.)
- **Python version guard** — refuses to start on Python 3.14+ (known import deadlock with the OpenAI SDK)
- **Git safety** — never force‑pushes, warns before hard resets, stashes before branch switches
- **Tool failure memory** — remembers past failures and injects them as context to avoid repeats

---

## 🔧 Development

```bash
# Syntax check
make lint

# Full verification (syntax + tool inventory)
make check

# Debug mode with format‑error traps
make debug

# First‑time API key setup
make init

# Rebuild venv after Python version change
make reinstall

# Git helpers
make git-status
make git-log
make commit msg='feat: description'
make push
```

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for details.
