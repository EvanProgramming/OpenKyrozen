# System One benchmark protocol

`benchmarks/system_one.py` is the repeatable typed-decision benchmark. It
uses the same fixed labeled corpus for routing, an explicitly supported
clarification, learning evidence, tool-output passages, and eight competing
memory candidates. It does not send the corpus text to diagnostics or store it
in the result file.

Each repetition uses the same cases. Decision rows report correctness,
accepted-decision coverage, abstentions, false decisions, decision latency,
token usage, Brier score, ECE, reliability bins, and repeated-decision
agreement. Memory rows report precision@3, recall@3, MRR@3, NDCG@3, and
effective rerank rate. Tool rows report precision, recall, specificity, F1,
warning rate, and quarantine rate. When `--url` is supplied, the existing
paired end-to-end harness additionally reports full-turn median/p95 latency,
LLM calls/tokens, answer agreement, cost, fallbacks, provider errors, and
warm/cold setup time.

The baseline is the current path: it abstains from typed decisions and keeps
the original memory order. Kev and Jev use the same questions and labels. Run
order is independent for typed calls; full-turn comparisons alternate the
backend order and create a fresh session for every side. Setup time is kept
outside turn latency.

```bash
# Reproducible three-repeat comparison (local Kev plus live Jev when configured)
python benchmarks/system_one.py --backends baseline,kev,jev --repeats 3 \
  > benchmarks/results/system_one_$(date +%F)_baseline_kev_jev.json

# Fit backend/action policies only after collecting a fixed labeled corpus.
# The command uses an 80/20 calibration/holdout split and writes only policies
# with pass/fail gates to ~/.kyrozen/system_one_calibration.json; only passing
# policies are applied at runtime.
python benchmarks/system_one.py --backends kev,jev --repeats 3 --calibrate

# Jev requires the environment key; it is never read from command-line text.
TYPESAFE_API_KEY=... python benchmarks/system_one.py \
  --backends baseline,jev --repeats 5 \
  > benchmarks/results/system_one_$(date +%F)_baseline_jev.json

# Full-turn paired measurement against one configured OpenKyrozen server.
python benchmarks/system_one.py --backend kev --repeats 3 --url http://127.0.0.1:8000 --mode ask
```

## Current calibrated comparison (2026-09-29)

This run expanded the fixed corpus to eight routing cases, three explicit
clarifications, six evidence cases, six tool-output cases, and three distinct
memory tasks. Each backend ran three repetitions. Calibration used grouped
80/20 splits, with tool labels stratified so both malicious and benign passages
appear in the holdout. The calibration raw rows and policies are in
[`benchmarks/results/system_one_2026-09-29_baseline_kev_jev_calibration.json`](../benchmarks/results/system_one_2026-09-29_baseline_kev_jev_calibration.json).
The second pass below measures the policies actually applied at runtime:
[`benchmarks/results/system_one_2026-09-29_baseline_kev_jev_applied.json`](../benchmarks/results/system_one_2026-09-29_baseline_kev_jev_applied.json).
The canonical dataset hash is `7438998a5681eaed`.

| Applied workload | Baseline | Kev-0.8B | Jev |
| --- | ---: | ---: | ---: |
| Typed raw accuracy | — | 91.3% | **100%** |
| Typed accepted precision / coverage | — / 0% | 100% / 47.8% | 100% / **87.0%** |
| Typed Brier / ECE | 0.250 / 0.370 | 0.1025 / 0.2325 | **0.0037 / 0.0319** |
| Typed repeated agreement | — | 100% | 100% |
| Decision median / p95 | 0 / 0 ms | 70.6 / 81.8 ms | 777.2 / 1,398.6 ms |
| Decision tokens (input + output) | 0 | 7,200 | 26,352 |
| Memory precision@3 / recall@3 | 0 / 0 | 0.556 / 0.556 | **1.000 / 1.000** |
| Memory MRR@3 / NDCG@3 | 0 / 0 | 1.000 / 0.666 | **1.000 / 1.000** |
| Memory effective rerank / promotion | 0% / 0% | 100% / 100% | 100% / 100% |
| Tool precision / recall / specificity / F1 | 0 / 0 / 1 / 0 | 1 / 1 / 1 / 1 | 1 / 1 / 1 / 1 |
| Tool quarantine / promotion | 0% / 0% | 50% / 50% | 50% / 50% |

The applied Jev pass accepted no false typed decisions and all five Jev action
policies passed their held-out gates: routing, clarification, learning evidence,
memory ranking, and tool quarantine. Kev passed routing, memory ranking, and
tool quarantine; its clarification and learning-evidence policies remain
advisory because their held-out coverage/quality gates did not pass. The memory
result is materially better than the previous 0/3 smoke reranks, but it is
still based on three labeled tasks and should be expanded before treating it as
a general accuracy guarantee. Tool scores use the calibrated positive-class
threshold and separate false-positive/recall gate.

The persisted probability thresholds are action-specific and come from this
dataset: Jev uses 0.98 for routing, 1.00 for clarification, 0.78 for evidence,
0.70 for memory relevance, and 0.050001 for tool quarantine; Kev uses 0.5694,
0.9225, 0.71, 0.70, and 0.305501 respectively. The last two tool thresholds
are the separating points just above the strongest benign score in each
calibration split. Kev clarification and evidence thresholds are recorded but
not active because their holdout gates failed.

## Full-turn DeepSeek comparison (2026-09-29)

These runs used the configured paid DeepSeek provider, Ask mode, fresh sessions,
and alternating off/on order. They measure full OpenKyrozen turns rather than
only the decision endpoint. Reply text is not stored; the raw rows contain a
short answer hash for repeated-agreement measurement.

| Metric | System One off | Kev | Jev |
| --- | ---: | ---: | ---: |
| Matched turns / keyword checks | 8 / 8 | 8 / 8 | 8 / 8 |
| Median / p95 turn latency | 3,781 / 9,771 ms | 4,470 / 6,180 ms | 4,282 / 5,681 ms |
| LLM calls | 8 | 8 | 8 |
| LLM tokens | 16,203 | 14,386 | 13,451 |
| Median System One decision latency | 0 ms | 121.1 ms | 1,140.7 ms |
| Repeated answer agreement | 0.625 | 0.625 | 0.625 |

Kev increased median turn latency by 18.3% but reduced recorded LLM tokens by
11.2% for this small paired workload. Jev increased median latency by 9.0% and
LLM tokens by 0.7%. Answer agreement did not improve for either backend. The
token reduction is an observed workload result, not a general cost guarantee;
the primary System One evidence is decision correctness and calibrated
uncertainty rather than speed.

Raw full-turn rows:
[Kev](../benchmarks/results/system_one_2026-09-29_fullturn_kev_deepseek.json) ·
[Jev](../benchmarks/results/system_one_2026-09-29_fullturn_jev_deepseek.json).

The live Jev server discovered `jev-latest` with release date
`2026-09-10T18:38:01.391457+00:00`; the `/v1/systemone` response resolved to
`jev-1.13.0`. Discovery is refreshed at startup and after 24 hours, and a
failed discovery safely falls back to the stable alias.

## Promotion rules

Policies are backend and action specific. Each policy records confidence and
probability thresholds, a probability margin, minimum calibration coverage,
and the ordinary-path fallback (`llm`, `user`, `candidate`, `existing_order`,
or `advisory`). A policy also needs at least three distinct labeled cases;
repeated calls cannot substitute for new cases. It is written only after its
held-out result passes the declared gate: routing precision at least 95%,
learning supportive precision at least 99% with no accepted contradictions,
memory NDCG@3 improvement of at least 0.10 absolute or 10% relative without a
precision@3 drop, and tool quarantine with zero false positives and at least
90% malicious-passage recall. Otherwise the result remains advisory and the
current OpenKyrozen path is used. The confidence value is recorded as a model
signal; it is not treated as an accuracy guarantee.
