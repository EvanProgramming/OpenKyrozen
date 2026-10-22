# OpenKyrozen V3 matched evaluation

The frozen cohort contains 30 fixtures across ten task categories, with three matched repeats per fixture on V2 `v2.0.8` (`1bfc82c793df49e76097061ec5adfef0e4e2e883`) and the V3 candidate. It includes dedicated replanning, Strategic Plan scope, material-revision approval, and plan-churn scenarios. `benchmarks/v3_matched_live.py` creates isolated workspace and state for each run, verifies outcomes outside the model, records token/request accounting, and resumes only a report whose hash is pinned in its original budget ledger. Process cancellation is checked against the OS process group or process tree; permission scenarios require denied runtime receipts and reject secret disclosure.

The local authenticated Responses relay pins `gemini-flash-3.6`, caps output at 4,096 tokens, reserves worst-case spend before forwarding, and stores request hashes and usage rather than prompts or answers. The SQLite ledger uses the supplied proxy settings and defaults to a cumulative ¥49 cap, below the approved ¥50 ceiling. Accounting uses ¥0.7/1M input, ¥3.7/1M output, ¥0.2/1M cache-write, and ¥0.1/1M cache-read tokens. Unknown usage retains its reservation. Set `KYROZEN_BENCHMARK_API_KEY` and `KYROZEN_BENCHMARK_BASE_URL` from the approved provider configuration; the default endpoint is `https://api.oproxy.world/v1`.

Before reading provider credentials or starting campaign requests, the runner probes the OS filesystem sandbox. The cohort requires working filesystem restrictions for its permission and scope checks, so an unavailable backend blocks the campaign without provider spend. This benchmark prerequisite does not change the application's best-effort sandbox default.

Run the complete cohort from a clean candidate checkout, writing the report and budget ledger outside it:

```sh
python -m benchmarks.v3_matched_live \
  --source . --repo . \
  --output /outside/checkout/v3-matched-report.json \
  --budget-db /outside/checkout/v3-matched-budget.sqlite3 \
  --limit-rmb 49
```

The report is descriptive evaluation data, not release authorization. A result is conclusive only when all 180 matched rows have complete provider usage, all independent verifiers pass, the full paired success interval improves, token and request reductions meet their thresholds, and no safety or protocol-recovery control fails. Missing or unknown usage, failed checks, budget exhaustion, or an incomplete run remains inconclusive. Any code change creates a different candidate SHA and requires a new matched run.
