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

## Structured response contract (V3 issue #228)

`LLMProvider.chat_response(messages, model=None)` returns `ModelResponse`:
`text`, ordered `ToolCall` objects, normalized `finish_reason`, usage and explicit
provider/model/response metadata. `ToolCall` preserves a native call ID, name and
JSON-object arguments. Contract validation raises `ProviderContractError`, a
`ValueError` subclass; fallback does not replay a received malformed response.
All processing after a successful SDK return is marked as the received-response
phase, so parsing or accounting failures cannot trigger another generation. IDs omitted by a transport are generated within the response
scope and listed in `metadata["synthesized_call_ids"]`. Malformed arguments,
duplicate IDs/JSON keys and non-JSON values are rejected; calls are never rendered
as assistant text or executed by the provider boundary.

Finish reasons distinguish `final`, `tool_request`, `length`, `provider_error`,
`cancelled`, `blocked` and `unknown`. The original status remains in metadata.
Absent or unfamiliar status does not prove completion. The existing `chat()`
tuple API delegates to this contract and refuses tool requests, incomplete,
blocked, cancelled or failed responses rather than silently losing their meaning.
Ordinary text retains its existing behavior. Direct Ollama `chat()` transport
failures now raise exceptions instead of returning an error-string tuple. Legacy-only providers are bridged
with unknown finish status and an explicit `legacy` marker.

`get_capabilities(model=None)` reports flags for `native_tools`, `strict_schemas`,
`parallel_calls`, `text_streaming`, `streaming_tool_calls` and `reasoning_controls`.
These describe usable features of the shipped adapter API, not every feature an
underlying model might support. Text streaming reflects the existing implementation;
other flags remain false until the corresponding request paths are implemented.
Fallback capabilities are the conservative intersection of the configured,
model-mapped candidates. Structured responses retain the responding provider's
metadata. A successful response rejected by text conversion does not cause another
provider request.

The runtime's `_get_model_response` preserves this object under the existing
bounded-call, usage and context scopes; `_get_llm_response` is the checked text
compatibility boundary. Existing stream methods remain text-only. Tool request
serialization, native history round trips and structured stream events belong to
[#229](https://github.com/EvanProgramming/OpenKyrozen/issues/229) and
[#230](https://github.com/EvanProgramming/OpenKyrozen/issues/230). This foundation
also relates to [provider adapter issue #225](https://github.com/EvanProgramming/OpenKyrozen/issues/225).
Offline fixtures and installed SDK types validate the contract; they do not establish
live provider interoperability.

## Optional dependencies

`pip install -e '.[web]'` installs FastAPI and Uvicorn. Provider extras are `.[claude]`, `.[gemini]`, `.[perplexity]`, `.[bedrock]`, and `.[vertex]`; `.[cloud]` groups the cloud integrations. `.[browser]` installs Playwright; browser execution also needs a browser installation. `.[all]` installs the supported optional Python integrations. The official installer has its own pinned set of dependencies; see [installation](installation.md).

## Costs and changing provider catalogs

The cost ledger attributes usage to the provider and model returned by the integration, including streaming usage when provided. Displayed costs can be estimates when a provider does not report token usage or published pricing is missing. Do not use a README model label or price as evidence that your account can access that model. See [usage and cost](usage-and-cost.md) for price provenance and interpretation.

## Provider failures and request limits

Provider failures raise SDK-independent `ProviderError`, exported with
`ProviderErrorKind`. Kinds distinguish transport, transport timeout, rate limit,
server failure, context overflow, authentication, invalid request, cancellation
and unknown failures. Errors retain provider/model identity, available HTTP
status and provider code, retry eligibility and bounded Retry-After information.
Displayed messages omit request bodies and credentials; the original exception
remains chained for private debugging and must not be exposed as a public traceback.
`ProviderContractError` remains a separate terminal response-validation failure.

Each logical request shares one wall-clock deadline and at most four transport
attempts across retries and fallback. Runtime defaults remain 90 seconds for
foreground calls and 180 seconds for child calls. `KYROZEN_PROVIDER_TIMEOUT_SECONDS`
accepts finite values clamped to 1–600 seconds; invalid/nonfinite values use the
default. Native SDK retries are disabled and transport timeouts use remaining time.
Standalone adapter calls use a 90-second request scope.

Transient transport, timeout, rate-limit and server failures use exponential
backoff from one second with jitter, respecting Retry-After within the deadline.
Fallback tries configured providers in order before repeating transient failures.
Authentication can advance to another provider but never repeats the failed
credentials. Invalid requests, context overflow, unknown failures, contract
failures, cancellation and deadline expiry stop immediately. This intentionally
replaces fallback on arbitrary exceptions. After a streaming chunk or a successful
response, failure never causes regeneration or provider switching.

Structured runtime callers receive typed errors. Existing text callers retain
`[LLM Error]` rendering and the existing single context-compaction recovery.
Provider retry does not execute or retry tools. Child cancellation and foreground
interruption stop waiting and prevent later transport attempts/output. Streams are
closed when the SDK permits; an uncooperative legacy transport may remain in a
daemon worker until it returns because Python cannot forcibly stop that thread.
