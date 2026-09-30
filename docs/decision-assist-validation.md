# Jev Decision and Decision Assist

Jev Decision is OpenKyrozen's optional judgment layer. It is not the main
chat model and it does not execute tools or approve work. Instead, it answers
small typed questions around five bounded jobs: request routing,
clarification, learning-evidence review, memory relevance, and suspicious
tool-output review.

The layer can accept a decision, abstain when confidence or privacy screening
is insufficient, or fall back to the existing OpenKyrozen path. Only policies
that pass their action-specific calibration gates are applied automatically;
otherwise the result stays advisory. Jev receives screened context through
paid TypeSafe calls, while local Kev is an optional lower-accuracy alternative
that requires explicit consent before it inspects private workspace context.

## Enable Jev Decision

From the terminal:

```text
/decision-assist jev
```

OpenKyrozen prompts for the Jev API key when one is not already configured and
stores it through the encrypted configuration flow. To inspect or change the
current state, use `/decision-assist`. To disable it, use
`/decision-assist off`.

For local Kev, use `/decision-assist kev yes`; the `yes` is the explicit
private-context consent. Revoke that consent with `/decision-assist revoke`.

This page records the validation evidence for Jev and Kev. The commands below
are non-mutating benchmarks: they record typed outcomes, confidence, latency,
and errors, but do not activate claims, reorder memories, quarantine tool text,
or execute a tool.

The current implementation asks candidate-specific evidence and memory
questions and classifies each instruction-like tool passage separately. The
older numbers below are retained as an audit of the pre-calibration rollout;
they do not enable a product gate. Use `benchmarks/system_one.py` for the
current calibrated comparison. The expanded 2026-09-29 run promotes all five
Jev actions and Kev routing, memory ranking, and tool review; Kev clarification
and evidence remain advisory until their holdout gates pass.

Run the labeled, non-mutating checks with either backend:

```bash
python benchmarks/decision_assist.py --backend kev --repeats 5
TYPESAFE_API_KEY=... python benchmarks/decision_assist.py --backend jev --repeats 5
```

The script records typed outcomes, confidence, latency, and errors only. It
does not activate claims, reorder memories, or quarantine tool text. Jev
cases are public and screened; live Jev results require an API key. A backend
that misses a quality gate must remain advisory for that check.

## Local Kev smoke (2026-09-27)

One live run against the loopback Kev-0.8B server (`jaredpalmer/kev-0.8b`,
revision `9a45d25eb2ab761841196625383fa1dff0e56c1e`) produced:

| Measure | Result |
|---|---:|
| Labeled checks | 4 |
| Correct judgments | 3/4 (75%) |
| Median decision latency | 98.78 ms |
| p95 decision latency | 400.17 ms |
| Evidence support | pass |
| Evidence contradiction | pass |
| Memory relevance top candidate | pass |
| Tool instruction quarantine threshold | fail; remained advisory |

This is a smoke sample, not an accuracy guarantee. The tool-output check stays
advisory until a larger labeled set demonstrates the configured high-confidence
threshold. Run the same command with `--backend jev` when Jev access is
available and compare correctness and latency separately.

## Integrated off-versus-Kev comparison (2026-09-27)

This run exercised the actual Decision Assist call sites after their existing
scope and privacy boundaries. It used three repetitions with alternating order:
Decision Assist off versus live local Kev, with fresh isolated evidence stores.
It did not promote claims, change memory state, grant permissions, or execute a
tool. The baseline records the current fallback path; Kev rows record the
typed call and the effective result.

| Integrated feature | Decision Assist off | Kev result | Median decision latency |
|---|---|---|---:|
| Learning evidence review | 9/9 abstentions; 0 calls; 0% coverage | 3/9 accepted, 100% accuracy on accepted decisions; 6/9 abstentions; 1,203 decision tokens | 134.43 ms |
| Memory relevance | Precision/recall/MRR/NDCG@3 all 0; 0 calls | Precision/recall/MRR/NDCG@3 all 0; 0/3 effective reranks; 3,186 decision tokens | 351.44 ms |
| Tool-output review | Recall 0%, specificity 100%, 0 calls; 6/9 candidate passages retained | Recall 0%, specificity 100%; 6/9 candidate calls produced ambiguous warnings; 0 quarantines; 498 decision tokens | 135.92 ms |

The full feature-call medians were 1.50 ms off versus 136.38 ms with Kev for
evidence review (p95 3.42 versus 467.86 ms), 0.12 versus 351.80 ms for memory
relevance (p95 0.14 versus 1,023.18 ms), and 0.12 versus 63.38 ms for
tool-output review (p95 0.27 versus 146.19 ms). The evidence result measures
accepted-decision coverage and accuracy separately: Kev was correct on all
three accepted support decisions, while it abstained on all contradiction and
insufficient cases. Memory ranking did not improve the labeled top three.
Tool review produced warnings but no high-confidence detections, so the
quarantine gate correctly remained advisory. Token totals and confusion counts
are in the raw JSON; this is a quality and overhead measurement rather than a
claimed product improvement.

Metric definitions: evidence coverage is accepted typed outcomes divided by
labeled cases, with accuracy calculated only over accepted outcomes; memory
precision/recall/MRR/NDCG@3 use the three labeled relevant memories; tool
precision/recall/specificity use the configured `noul >= 0.90` threshold, while
warnings and quarantines are reported as separate effective actions.

Raw data: [decision_assist_compare_2026-09-27_kev.json](../benchmarks/results/decision_assist_compare_2026-09-27_kev.json).

Repeat the same comparison with local Kev or, when configured, Jev:

```bash
python benchmarks/decision_assist_compare.py --backend kev --repeats 3
TYPESAFE_API_KEY=... python benchmarks/decision_assist_compare.py --backend jev --repeats 3
```
