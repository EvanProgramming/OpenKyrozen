# OpenKyrozen V3 matched evaluation

The frozen cohort contains 30 fixtures across ten task categories, with three matched repeats per fixture on V2 `v2.0.8` (`1bfc82c793df49e76097061ec5adfef0e4e2e883`) and the V3 candidate. It includes dedicated replanning, Strategic Plan scope, material-revision approval, and plan-churn scenarios. `benchmarks/v3_matched_live.py` creates isolated workspace and state for each run, verifies outcomes outside the model, records token/request accounting, and resumes only a report whose hash is pinned in its original budget ledger. Process cancellation is checked against the OS process group or process tree; permission scenarios require denied runtime receipts and reject secret disclosure.

The local authenticated Responses relay pins `gemini-3.5-flash-lite`, caps output at 4,096 tokens, reserves worst-case spend before forwarding, and stores request hashes and usage rather than prompts or answers. It moves system-role messages from `input` to Responses `instructions` for both benchmark arms; when no other input remains, it sends `Continue.`. It stops consuming a stream at its terminal event, preserving unknown usage as a held reservation without attempting to settle the same attempt twice. Worker failures record the exit code and exception class without persisting stderr or its potentially sensitive details. The cumulative cap is US$10. Panstar's quoted rates are $0.033/1M input, $0.275/1M output, and $0.0033/1M cache reads; cache writes are charged at the input rate because Panstar quoted no separate rate. The CNY SQLite ledger uses the recorded 2026-10-22 USD/CNY rate of 6.6759 and records both the quote and FX source in pricing metadata. Unknown usage retains its reservation. Cumulative prior exposure through candidate `aa12e40`, including prior probes, matched runs, interruptions, settled charges, and retained reservations, totals ¥2.170911126525; it is imported as a conservative held reservation, and every later attempt must carry its entire exposure forward. The application-shaped native tool continuation was verified; a separate `previous_response_id` diagnostic was rejected by the gateway and is not used by OpenKyrozen. Set `KYROZEN_BENCHMARK_API_KEY` and `KYROZEN_BENCHMARK_BASE_URL` from the approved provider configuration; the default endpoint is `https://ai.panstar.ai/v1`.

Before reading provider credentials or starting campaign requests, the runner probes the OS filesystem sandbox. The cohort requires working filesystem restrictions for its permission and scope checks, so an unavailable backend blocks the campaign without provider spend. This benchmark prerequisite does not change the application's best-effort sandbox default.

Run the complete cohort from a clean candidate checkout, writing the report and budget ledger outside it:

```sh
python -m benchmarks.v3_matched_live \
  --source . --repo . \
  --output /outside/checkout/v3-matched-report.json \
  --budget-db /outside/checkout/v3-matched-budget.sqlite3 \
  --limit-usd 10
```

The report is descriptive evaluation data, not release authorization. A result is conclusive only when all 180 matched rows have complete provider usage, all independent verifiers pass, the full paired success interval improves, token and request reductions meet their thresholds, and no safety or protocol-recovery control fails. Missing or unknown usage, failed checks, budget exhaustion, or an incomplete run remains inconclusive. Any code change creates a different candidate SHA and requires a new matched run.
