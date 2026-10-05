# Configuration

OpenKyrozen has separate provider connection settings and workspace behavior settings. Provider credentials come from their provider-specific environment variable, ambient cloud identity, or the encrypted user configuration flow. `agent.yaml` never stores provider secrets. The strict YAML loader rejects duplicate and unknown keys.

## Agent configuration files and precedence

The application starts with packaged defaults, overlays the active workspace's `agent.yaml`, overlays the file selected by `KYROZEN_AGENT_CONFIG` (or by an explicit loader argument), then applies recognized environment overrides. Later layers take precedence for values they set. Relative workspace configuration stays inside the workspace; an explicitly selected file must exist.

The version 1 YAML object accepts:

| Key | Accepted shape |
|---|---|
| `version` | Integer `1`. |
| `provider` | `name`, `model`, and `max_tokens`; `name` must be a registered provider and `max_tokens` is an integer from 1 to 1,000,000. |
| `role` | Required `name` and `system` text. |
| `instructions` | Text appended as agent guidance. |
| `examples` | Up to 12 example mappings with user and assistant text. |
| `capabilities` | Non-empty list of recognized labels or profiles; this is an upper bound, not a permission grant. |
| `subagents` | `concurrency` from 1 to 64 and optional per-profile `provider`/`model` role defaults. |

Unknown keys, duplicate YAML mapping keys, invalid types, and out-of-range values fail configuration loading. A minimal example is [`agent.yaml`](../agent.yaml). For agent role text, treat examples and project instructions as untrusted inputs; they cannot expand effective runtime capabilities.

## Environment variables

| Variable | Purpose |
|---|---|
| `KYROZEN_PROVIDER`, `KYROZEN_API_KEY`, `KYROZEN_BASE_URL` | Select the ordinary provider and, where supported, supply generic credentials/endpoint. Provider-specific credential variables take precedence where configured. |
| `KYROZEN_MODEL_SIMPLE`, `KYROZEN_MODEL_COMPLEX`, `KYROZEN_MODEL_MAIN`, `KYROZEN_CONTEXT_WINDOW_TOKENS`, `KYROZEN_PROVIDER_TIMEOUT_SECONDS` | Override model choices, pin one main-agent model, set a custom-model context limit, and set provider timeout. |
| `KYROZEN_AGENT_CONFIG` | Select the explicit `agent.yaml` overlay. |
| `KYROZEN_AGENT_PROVIDER`, `KYROZEN_AGENT_MODEL` | Override provider name/model in the agent role configuration layer. |
| `KYROZEN_ROLE`, `KYROZEN_ROLE_PROMPT`, `KYROZEN_INSTRUCTIONS`, `KYROZEN_EXAMPLES`, `KYROZEN_AGENT_CAPABILITIES` | Override agent role/instruction/example content or configured upper-bound capability labels. Examples must be a JSON list. |
| `KYROZEN_DB_PATH`, `KYROZEN_VECTOR_PATH`, `KYROZEN_DISABLE_VECTOR_INDEX` | Select the authoritative SQLite database, derived Chroma index, or disable the optional vector index. The default Chroma directory is a sibling named `chroma_index_v2`. |
| `KYROZEN_WORKSPACE_ROOT`, `KYROZEN_LAUNCH_MODE`, `KYROZEN_WORKSPACE_ID` | Configure workspace binding and storage scope. Command-line project selection takes precedence for the launch workspace. |
| `KYROZEN_SERVER_TOKEN`, `KYROZEN_SERVER_ACTOR`, `KYROZEN_AUDIT_LOG` | Protect remote web/API access, set the single server actor, and override the audit log destination. |
| `KYROZEN_DECISION_ASSIST_BACKEND` | Set the optional Decision Assist default backend (`off`, `jev`, or `kev`). |
| `KYROZEN_WEB_CAPABILITIES`, `KYROZEN_MCP_CAPABILITIES`, `KYROZEN_MCP_ALLOW_DANGEROUS` | Configure the server tool profiles; dangerous MCP opt-in selects the full profile. Read [security](security.md) before changing them. |
| `KYROZEN_APPROVAL_MODE`, `KYROZEN_EXECUTION_SURFACE`, `KYROZEN_TUI_CAPABILITIES` | Runtime approval and UI surface controls. They do not change the OS account's permissions or turn off policy intersections. |
| `KYROZEN_ALLOW_DYNAMIC_TOOLS` | Explicitly allow the separately gated dynamic tool feature. Review [extensions](extensions.md) before enabling it. |
| `KYROZEN_LEARNING_WORKER`, `KYROZEN_LEARNING_OLLAMA_BASE_URL`, `KYROZEN_LEARNING_CONSTITUTION` | Learning worker/runtime configuration, local Ollama endpoint, and local learning policy input. |
| `KYROZEN_PROMPT_PROFILE` | Select the optional compact prompt/discovery profile; default is `classic`. |
| `KYROZEN_SKILLS_DIR`, `KYROZEN_BROWSER_PROFILES`, `KYROZEN_BROWSER_ALLOW_PRIVATE` | Select local skill and browser profile storage or explicitly permit private browser destinations. Treat those destinations and instructions as sensitive. |
| `KYROZEN_GH_BINARY`, `KYROZEN_TUI_BINARY`, `KYROZEN_BACKEND_COMMAND`, `KYROZEN_BACKEND_PYTHON`, `KYROZEN_BACKEND_MODULE`, `KYROZEN_DISABLE_TUI` | Locate packaged/helper commands or force the Rich terminal fallback. These are mainly installer, test, and integration settings. |
| `KYROZEN_TTS_TEXT`, `KYROZEN_MEMORY_MAX_LOGS` | Optional spoken text and bounded memory/log behavior. |

The variable list is the supported, documented configuration surface at the v2.0.6 release snapshot. Variables marked as helper or test controls can change as implementations evolve; inspect the relevant entry point before building long-lived deployment tooling around them.

## Provider selection

```bash
export KYROZEN_PROVIDER=deepseek
export DEEPSEEK_API_KEY=your-key
kyrozen
```

Other supported provider families include OpenAI, Anthropic, Google, Ollama, Z.AI, Moonshot/Kimi, OpenRouter, Groq, Mistral, xAI, Together, Fireworks, Cohere, Azure OpenAI, Perplexity, Bedrock, and Vertex.

Switch interactively with /provider. Ollama can run without a hosted API key:

```bash
export KYROZEN_PROVIDER=ollama
export KYROZEN_BASE_URL=http://127.0.0.1:11434/v1
```

## Model selection

OpenKyrozen has separate defaults for simple and complex work:

```bash
export KYROZEN_MODEL_SIMPLE=deepseek-flash
export KYROZEN_MODEL_COMPLEX=deepseek-v4-pro
```

Set `KYROZEN_MODEL_MAIN` or use `/model <name>` to pin every main-agent request to one model. Use `/model auto` to return to the simple/complex choices. Sub-agent model assignments remain independent. Ollama requires an explicit installed model tag; `/model` shows discovered tags as suggestions and accepts a manually entered tag.

These names are the repository's DeepSeek registry defaults at the v2.0.6 documentation snapshot. They are not a promise that a provider account enables a particular model. The [provider guide](providers.md) lists every current selectable family and its repository default; choose a model your account can access.

The model-visible context window can be set explicitly when a custom model is not recognized:

```bash
export KYROZEN_CONTEXT_WINDOW_TOKENS=128000
```

## Sub-agent providers and concurrency

`agent.yaml` accepts `subagents.concurrency` (default 4, integer 1–64) and
`subagents.roles.<profile>.provider/model`. Additional assignments queue; there
is no total-agent limit. Assignment overrides take precedence over role defaults,
then the main provider/model. A reviewer uses the `reviewer` role default.

```yaml
subagents:
  concurrency: 4
  roles:
    researcher:
      provider: openai
      model: your-openai-model
    reviewer:
      provider: anthropic
      model: your-claude-model
```

Configure `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` securely in the launching
environment. Alternate providers never receive the main provider's generic
`KYROZEN_API_KEY`, endpoint or model settings. Missing credentials block the run;
there is no implicit cross-provider fallback. See [sub-agents](subagents.md).

## Web server

```bash
kyrozen-web --host 127.0.0.1 --port 8000
KYROZEN_SERVER_TOKEN=change-me kyrozen-web --host 0.0.0.0 --port 8000
```

Use a token whenever the server is reachable beyond localhost. The Docker image uses /app as its project root; mount persistent state separately when deploying it.

## Learning provider

Learning starts in setup_required. Choose Local or Remote from /self-learning or the web API. Local uses an explicitly approved local Ollama model; Remote reuses the configured chat provider and labels usage as surface=learning. Deterministic indexing, retrieval, scoring, and outcome recording do not require an LLM.

## State locations

| Path | Purpose |
| --- | --- |
| ~/.kyrozen_config.json | Encrypted provider configuration |
| ~/.kyrozen/workspace | Default global workspace |
| ~/.kyrozen/v2 | SQLite state, graphs, tools, and managed runtime data |
| chroma_memory/ | Local rebuildable vector index in a source checkout |

Keep venv/, chroma_memory/, build output, and generated package metadata out of commits. See the security notes in README.md and the project guidelines in AGENTS.md.
