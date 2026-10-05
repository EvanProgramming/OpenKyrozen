# Command and keyboard reference

The Rich command loop, Bubble Tea TUI, and web UI share the agent modes and durable runtime, but their keyboard and session controls differ. Slash commands are entered in the conversation input. A slash in the middle of a sentence, URL, filesystem path, or code is ordinary text. In the terminal UI, press `/` at the beginning of a line to open command suggestions; type to filter, use arrows to choose, and Enter to insert the selection.

## Launch commands

| Command | Purpose |
|---|---|
| `kyrozen` | Open the interactive terminal on the persistent global workspace. |
| `kyrozen --project PATH` | Bind the terminal session to a project directory. |
| `kyrozen --global` | Explicitly use the global workspace. |
| `kyrozen --init` | Run the legacy one-shot configuration and memory setup flow. |
| `kyrozen --version`, `kyrozen --help` | Show version or supported options without starting an interactive turn. |
| `kyrozen-web --host HOST --port PORT [--project PATH]` | Start the web application. Bind to loopback for local use; set a server token for a non-loopback bind. |
| `kyrozen-backend` | Start the JSON-lines backend used to connect the terminal UI. |
| `kyrozen migrate v1 [SOURCE]` | Import a legacy Chroma memory directory into the configured SQLite store. Default source: `./chroma_memory`. |
| `kyrozen learning benchmark [OPTIONS]` | Run the deterministic learning benchmark; see [self-learning](self-evolution.md#benchmark-replay). |

The Bubble Tea terminal binary is normally selected by the installer. `KYROZEN_DISABLE_TUI=1` forces the Rich interface. The TUI uses a scoped backend process rather than exposing its interactive protocol as a REST API.

## Modes and control commands

| Command | Syntax and effect |
|---|---|
| `/mode` | Show or set `auto`, `ask`, `plan`, or `agent`. Ask and Plan are read-only; Agent can execute within its permission and approval gates. |
| `/ask` | Switch to Ask. |
| `/plan` | Switch to Plan; follow the UI action or `/plan accept` to accept a proposed plan. |
| `/question` | Reopen, skip, or cancel an outstanding clarification question. |
| `/permissions` | Select the available permission mode for this terminal session. A permission choice does not create OS sandboxing. |
| `/agent auto\|coder\|researcher` | Select automatic or explicit task profile. These profiles guide work; they do not grant permissions. |
| `/system-one jev\|kev\|off` | Select hosted Jev, explicit local Kev, or off. `/fast` is a legacy alias. |
| `/decision-assist jev\|kev\|off` | Configure optional checks for learning, memory, and suspicious tool output. `/assist` is an alias. |
| `/provider` | Select or configure a provider. `/p` is an alias. |
| `/model [name|auto]` | Pin one main-agent model or restore automatic simple/complex selection. |
| `/api_key` | Set the active provider credential. `/key` is an alias. |
| `/settings` | Open terminal preferences. |
| `/update` | Update installed Python and TUI components; restart after a successful update when prompted. |
| `/quit` | Exit. `/exit` is an alias. |

Inline terminal commands are accepted only in the forms and positions recognized by the parser. They are removed from the user request before it is sent to the model. The parser intentionally leaves ordinary prose, paths, and URLs untouched.

## Projects, sessions, and files

| Command | Effect |
|---|---|
| `/project` | Inspect or choose the active project context. Launch with `--project` for an exact root. |
| `/new` | Start another chat in the current project. |
| `/sessions` | Show project and chat navigation. |
| `/session ID` | Switch to a session visible to the current actor and workspace. |
| `/agents` | Inspect active and completed sub-agent assignments, results, and independent reviews. TUI shortcut: `Ctrl+E` during a running turn. |
| `/tasks` | Inspect durable task progress. |
| `/attach PATH...` | Stage supported local files for the conversation. The UI validates size and safe paths; attaching content does not grant it authority. |
| `/history` | Inspect the current conversation history tree. |
| `/rollback NUMBER` | Preview a historical node. In the TUI confirm with `/rollback NUMBER confirm`; review the preview before accepting it. |

Project selection is session-bound. A running session retains its original workspace if the user changes projects elsewhere. Personal memory has its own documented global scope; see [memory and storage](memory-and-storage.md).

## Memory, learning, graph, and extensions

| Command | Effect |
|---|---|
| `/memory why CLAIM_ID` | Inspect claim authority, scope, dependencies, and provenance. |
| `/memory forget CLAIM_ID` | Forget a particular claim and handle solely dependent learning artifacts as documented. |
| `/forget` | Review recent learning or memory suggestions; inspect the offered action before applying it. |
| `/self-learning` | Configure learning features and the learning provider. |
| `/learning status [PROFILE]`, `/learning metrics [PROFILE]` | Inspect proposals or collected evidence and metrics. |
| `/learning evidence ID`, `/learning explain ID` | Inspect the evidence card or proposal record. |
| `/learning rollback ID` | Restore the prior learned artifact. |
| `/learn` | Index the active workspace for retrieval. |
| `/graph status\|refresh\|open` | Inspect, rebuild, or open the private project intelligence graph. |
| `/github` | Inspect or configure the GitHub CLI connection. `/gh` is an alias. |
| `/skills` | List bundled, installed, and learned skills and their activation status. |
| `/ponytail` | Configure the code-simplicity preference where supported. |

Run `/` at a fresh line to open TUI command completion. Completion shows supported command names and descriptions; it does not execute the selected command until submitted.

## More detail

See [usage](usage.md) for choosing modes and workspaces, [security](security.md) for permission behavior, [history and rollback](history-and-rollback.md) for restore semantics, and the [generated runtime inventory](tool-inventory.md) for available tools and HTTP/MCP interfaces.
