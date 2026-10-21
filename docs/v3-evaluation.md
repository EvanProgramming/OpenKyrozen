# OpenKyrozen V3 matched evaluation

The frozen cohort contains 30 fixtures across ten task categories, with three matched repeats per fixture on V2 `v2.0.8` (`1bfc82c793df49e76097061ec5adfef0e4e2e883`) and the V3 candidate. It includes dedicated replanning, Strategic Plan scope, material-revision approval, and plan-churn scenarios. `benchmarks/v3_matched_live.py` creates isolated workspace and state for each run, verifies outcomes independently, records complete token/request accounting, and resumes only reports bound to the same candidate and fixture set.

The local authenticated Responses relay pins `gemini-3.5-flash-lite`, caps output at 4,096 tokens, reserves worst-case spend before forwarding, and stores request hashes and usage rather than prompts or answers. The SQLite ledger defaults to the approved cumulative US$20 cap. Unknown usage retains its reservation. Set `PANSTAR_API_KEY` or configure the `Panster` custom provider profile; `PANSTAR_BASE_URL` may override the default endpoint.

Run the complete cohort from a clean candidate checkout, writing the report and budget ledger outside it:

```sh
python -m benchmarks.v3_matched_live \
  --source . --repo . \
  --output /outside/checkout/v3-matched-report.json \
  --budget-db /outside/checkout/v3-matched-budget.sqlite3
```

The report is descriptive evaluation data, not release authorization. A result is conclusive only when all 180 matched rows have complete provider usage, all independent verifiers pass, the full paired success interval improves, token and request reductions meet their thresholds, and no safety or protocol-recovery control fails. Missing or unknown usage, failed checks, budget exhaustion, or an incomplete run remains inconclusive. Any code change creates a different candidate SHA and requires a new matched run.
