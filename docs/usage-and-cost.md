# Usage, token accounting, and estimated cost

OpenKyrozen records provider usage locally when the provider returns token counts. The ledger attributes completed calls to the provider and model used, including the returned model for streaming responses. Feature and learning calls can be labeled separately from ordinary chat. Use the `/api/cost` endpoint and the terminal startup summary to inspect recorded totals.

## What a count means

Input tokens are the usage reported by the provider for the request, which may include repeated prompt context, cached-input details, or provider-specific categories. Output tokens count the response usage returned by the provider and can include hidden reasoning categories for integrations that report them. Provider data may be absent, delayed, or represented differently across transports. A local estimate is not a provider invoice.

The SQLite usage ledger is the durable source for supported records. Interactive cost resets affect the requested ledger scope, not the external provider account or its billing history. The `/api/cost/reset` action is destructive for the selected local counters; inspect the scope before invoking it. In multi-session installations, distinguish the installation, actor, workspace, and session totals where the response offers them.

## Price estimates

Prices and aliases can change over time, by provider, account tier, region, cache class, batch mode, or effective date. Where the cost calculator applies a price schedule it retains effective-time handling for supported providers. If a provider/model has no recognized price or returned usage detail, reported currency values can be zero, estimated, or incomplete even when usage happened. Read the provider's billing page for a chargeable amount and the repository's [provider registry](../openkyrozen/providers/registry.py) for code defaults.

This guide intentionally does not reproduce a complete rate card: consult the provider's official pricing source at the time of use. Do not compare models using the built-in estimator as if every hidden token, promotion, tax, or account discount had been counted.

## Useful checks

- If token counts are zero, inspect the provider response and usage receipt, not just the displayed currency.
- If a stream appears without cost, confirm that the provider emitted final usage and that the turn completed.
- If a model name changes after fallback, attribute usage to the resolved model in the receipt.
- If one feature seems to consume chat usage, distinguish its `surface`/feature label and event scope before drawing a conclusion.
- Use `/learning metrics` for evidence about learning calls and `/api/cost` for ledger totals.

For controlled comparisons, see the reproducible fixture protocol in [self-learning benchmarks](self-evolution.md#benchmark-replay) and the dated [System One benchmark](system-one-benchmark.md). Benchmark results are not general claims about response quality, latency, or future cost.
