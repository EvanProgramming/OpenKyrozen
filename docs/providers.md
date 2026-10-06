# Model providers

OpenKyrozen separates provider selection, credentials, model defaults, and task-complexity routing. The provider registry in `openkyrozen/providers/registry.py` is the source for selectable names and repository defaults. A model shown here is OpenKyrozen's configured default; accounts, regions, and provider catalogs can change independently. Override a model when the selected provider does not expose its default in your account.

## Current registry defaults

| Name | Credential or identity source | Simple / complex default | Optional extra |
|---|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-flash` / `deepseek-v4-pro` | Core |
| `openai` | `OPENAI_API_KEY` | `gpt-6-luna` / `gpt-6-astra` | Core |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` / `claude-fable-5-1` | `claude` |
| `google` | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` / `gemini-3.1-pro-preview` | `gemini` |
| `ollama` | None; local server | User-selected local tag | Core client; local Ollama and an explicitly selected model required |
| `glm` | `ZAI_API_KEY` | `glm-5.3-flash` / `glm-5.3` | OpenAI-compatible transport |
| `kimi` | `MOONSHOT_API_KEY` | `kimi-k2.6` / `kimi-k3` | OpenAI-compatible transport |
| `openrouter` | `OPENROUTER_API_KEY` | `~openai/gpt-sol-latest` / same | OpenAI-compatible transport |
| `groq` | `GROQ_API_KEY` | `openai/gpt-oss-120b` / same | OpenAI-compatible transport |
| `mistral` | `MISTRAL_API_KEY` | `mistral-small-2603` / same | OpenAI-compatible transport |
| `xai` | `XAI_API_KEY` | `grok-4.7` / same | OpenAI-compatible transport |
| `together` | `TOGETHER_API_KEY` | `MiniMaxAI/MiniMax-M3` / same | OpenAI-compatible transport |
| `fireworks` | `FIREWORKS_API_KEY` | `accounts/fireworks/models/deepseek-v3p1` / same | OpenAI-compatible transport |
| `cohere` | `COHERE_API_KEY` | `command-a-plus-05-2026` / same | OpenAI-compatible transport |
| `azure_openai` | `AZURE_OPENAI_API_KEY` plus Azure endpoint/deployment configuration | Account-specific | `azure` |
| `perplexity` | `PERPLEXITY_API_KEY` | `perplexity/sonar` / same | `perplexity` |
| `bedrock` | AWS SDK credential chain and region | Account-specific model ID | `bedrock` |
| `vertex` | Google Cloud identity and project/location | Account-specific model ID | `vertex` |
| `custom` | Optional API key in encrypted profile storage | Manually entered simple / complex model IDs | Core OpenAI-compatible client |

Confirm names, default values, credential lookup, and optional extras against the current [provider registry](../openkyrozen/providers/registry.py), [`pyproject.toml`](../pyproject.toml), and [configuration](configuration.md) before changing them. Providers that use deployment names, ambient credentials, or regional catalogs intentionally leave model defaults blank in the registry.

## Configure credentials and defaults

Set the provider and its credential in the shell that starts OpenKyrozen:

```sh
export KYROZEN_PROVIDER=deepseek
export DEEPSEEK_API_KEY=replace-with-your-key
export KYROZEN_MODEL_SIMPLE=deepseek-flash
export KYROZEN_MODEL_COMPLEX=deepseek-v4-pro
kyrozen
```

The interactive provider and `/api_key` flows can save credentials through OpenKyrozen's encrypted user configuration. Treat that file as private data. Environment settings are useful for servers and automation; do not place keys in `agent.yaml`, project files, command-line arguments, shell history, or issue reports.

OpenAI-compatible services can use `KYROZEN_BASE_URL` where the selected integration supports it. Ollama normally listens at `http://localhost:11434/v1`; override its endpoint and model names if your server differs. Azure OpenAI, Bedrock, and Vertex need account-specific resource/deployment, region, or identity configuration; installing the optional Python extra alone does not provision cloud access.

Use `/custom-provider create` to add a named OpenAI-compatible endpoint with its optional key, endpoint URL, simple and complex model IDs, and context limit. `/custom-provider list`, `edit <name>`, `use <name>`, and `remove <name>` manage profiles. The TUI provider picker opens the same guided setup with masked key entry. Profiles are stored in `~/.kyrozen_config.json`; API keys are encrypted. A sub-agent role can select a profile with `provider: custom` and `custom_profile: <name>` in `agent.yaml`.

## Model routing and fallback

The normal provider setting has separate simple and complex model slots. The agent chooses between them using its task routing policy. `/model <name>` pins the main agent to one model; `/model auto` restores complexity routing. Sub-agent model assignments remain independent. Ollama never selects an installed model automatically: setup lists installed tags as suggestions and requires a chosen tag. `KYROZEN_CONTEXT_WINDOW_TOKENS` supplies a context size when an otherwise unknown custom model cannot be detected; use the actual provider limit, including the space needed for output.

The registry marks a subset of providers for automatic selection and records preferred fallback providers. A fallback is not guaranteed: it still needs credentials, an available compatible model, and a supported transport. Alternate sub-agent providers use their own configured environment credentials and do not inherit a generic main-provider key. See [sub-agents](subagents.md) before using cross-provider assignments.

## Optional dependencies

`pip install -e '.[web]'` installs FastAPI and Uvicorn. Provider extras are `.[claude]`, `.[gemini]`, `.[perplexity]`, `.[bedrock]`, and `.[vertex]`; `.[cloud]` groups the cloud integrations. `.[browser]` installs Playwright; browser execution also needs a browser installation. `.[all]` installs the supported optional Python integrations. The official installer has its own pinned set of dependencies; see [installation](installation.md).

## Costs and changing provider catalogs

The cost ledger attributes usage to the provider and model returned by the integration, including streaming usage when provided. Displayed costs can be estimates when a provider does not report token usage or published pricing is missing. Do not use a README model label or price as evidence that your account can access that model. See [usage and cost](usage-and-cost.md) for price provenance and interpretation.
