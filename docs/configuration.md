# Configuration

OpenKyrozen accepts provider settings through environment variables or the encrypted user configuration flow. Do not put keys in agent.yaml, source files, commits, or issue reports.

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
