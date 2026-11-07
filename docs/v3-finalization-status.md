# V3 finalization status

V3 is **unpublished**. This document describes implemented safeguards, retained investigation results and required acceptance evidence. Release readiness is established by the external, signed R250–R266 bundle for the exact clean candidate; source changes and intermediate passing tests do not establish it. The final workflow stops before creating a V3 tag or Release.

## Implemented safeguards

- Matched evaluation now retains independent scope, original-test integrity, provider, process and worker-completeness evidence. Missing evidence fails closed. The first safety failure is checkpointed and stops the campaign; resuming cannot skip it. Malformed worker JSON is preserved as an incomplete result rather than losing the checkpoint.
- Worker answers, receipts, task state and plan events require durable, parent-bound artifacts for independent review after workspace cleanup. Resume and completeness checks reject missing, modified or cross-row evidence. Protection probes use isolated temporary homes and a minimal environment before any provider credential lookup.
- DeepSeek has immutable CNY pricing, a cumulative 15 CNY cap, serial requests and fixed 2.032768 CNY worst-case reservations. Original source ledgers are read-only, copies deduplicate, and proven carried history expands to original attempt records. Smoke, retries and cohort share the ledger; unknown use stays reserved.
- New and missing DeepSeek model slots both default to `deepseek-flash`; explicit saved model choices remain effective. Equal default slots do not demonstrate a model-selection benefit.
- A two-request native-tool smoke checks unpredictable arguments, correlated function-call output, final response and complete usage before the 180-row cohort layout.
- Original fixture tests are OS-protected for both benchmark arms. The benchmark-only external shell broker enforces parent-owned bounds and request-bound tickets. Real macOS tests cover legitimate writes, protected-file attacks, narrow scope, output/exit/cancellation/timeout and frozen V2 compatibility.
- Process lifecycle audit persists scoped launch intent before dispatch and retains uncertain outcomes across restart. Historical PIDs are observation metadata, never authority to reattach, signal or replay. Audit failure cannot certify cleanup, and bounded audit writes preserve cancellation under SQLite contention.
- Learning promotion requires complete paired outcome, verification, cost, latency and regression measurements. Missing dimensions and old success-only replay records remain incomplete. Known failed effects replace unsupported final success claims with receipt-derived results, including valid structured child reports.
- Public README, installation guide and website commands remain on the published v2.0.8 assets. Candidate package metadata remains 3.0.0. V3 publication must precede switching public download instructions.

## Required release evidence

1. **Final actor/platform evidence:** fresh-process delegation, kernel UID ownership, immutable parent policy, read-scope protection and UID-based detached-process cleanup are implemented. All four Linux/macOS Python combinations passed the privileged suite for intermediate commit `c87ecef`; the earlier six-platform workflow also completed all six 30-minute soaks. Both results remain historical after source changes. The release bundle must establish all of these checks for the final clean candidate.
2. **R260 history and causal closure:** the original failure was a five-second future-completion timeout after cancellation. Reconstructed Python 3.12.10 and dependency pins exposed foreground SQLite close latency. A pinned old-source controlled regression reproduces the timeout, and the owned-connection implementation avoids that foreground last-close operation. An unmodified old-source near-stall spent most of its time inside SQLite with little thread CPU time. These observations establish a defect and remediation, but cannot distinguish the historical storage wait from scheduler delay. The original failure had no worker stack. Two subsequent native Windows captures remained incomplete or fast and did not establish its cause; ordinary Windows acceptance and repeated passes cannot close this requirement. A bounded custom native trace retains scheduling and file-I/O signals without changing the original test deadlines or collection limits. Actual Windows parser, zero-loss capture and causal analysis evidence must be provided in the exact-candidate bundle.
3. **Complete acceptance:** the release bundle must establish default-on routing benefit, requirement-by-requirement acceptance, final six-platform/30-minute validation, the live 180-row comparison and all 17 signed passing records. Main must then point to that same verified SHA without rewriting it.

The full campaign requires a privileged disposable runner and executes real actor and descendant checks before credential lookup or model requests. The optional bounded component probe returns explicitly incomplete isolation evidence and cannot replace that preflight.

## Functional acceptance audit

The 266 source criteria were independently audited against implementation and regression bodies: 196 implemented-with-regression, 52 partial and 18 unproven at the audit baseline. Partial includes both concrete functional gaps and missing acceptance evidence. Those baseline counts are not a final acceptance result. Issue closure alone is not completion.

Implemented repairs cover unsuccessful provider finishes, native streaming, explicit task start/completion, immutable strategic objective anchors with scoped refinement, pinned Working Plans, unknown-effect restart protection, turn-scoped background cancellation, advisory-only Jev decisions and evidence-gated learning promotion. Strategic transitions now belong to `tasks.planning.PlanningService`; the interaction controller retains mode and question adaptation.

Native Agent, Ask and Plan paths use typed execution/control contracts. Plan proposals preserve explicit user acceptance, material revisions pause mutations, and safe narrowing preserves the existing automatic-acceptance policy. Lazy legacy helpers and reply-specific provenance keep literal native text intact through CLI/Web/TUI formatting and delegated synthesis. Complex read-only children can edit only their own internal plans and retrieve only their own tool artifacts, without receiving filesystem write authority.

Native context preflight now includes tool schema and continuation payload estimates, binds reported counts to provider/model/schema identity, and compacts old complete history units without splitting call/result groups. Overflow recovery retries only generation, once, before any emitted stream content; it cannot replay an executed tool. Prose-only task criteria remain pending unless trusted verification exists: a successful file write cannot certify an unrelated test or business outcome.

The repaired 30-fixture cohort and observer/verifier identities are frozen before any paid run. Multi-file/API, refactor, Git and multi-turn context oracles retain the original comparison thresholds and shared physical success criteria. New regression, packaging and platform evidence must bind the eventual clean signed candidate; intermediate passing suites cannot be combined into a final release manifest.

## Evidence and constraints

- Approved execution plan: [V3 finalization](superpowers/plans/2026-11-07-v3-finalization.md).
- Windows candidate diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261579117
- Windows historical diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261682019
- Historical Windows load controls: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42288273719
- Intermediate four-platform privileged isolation: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42349253524
- Incomplete continuous Windows native capture: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42350403044
- Detailed local execution logs, 266 requirement/comment inventory and historical Windows artifacts are outside the checkout in `/Users/evangong/.codex/worktrees/openkyrozen-v3-evidence/`.
- Original DeepSeek ledgers independently reconstruct settled 0.58291496 CNY plus held 0.000000000007 CNY, total 0.582914960007 CNY. This work has made no external DeepSeek requests.

Do not waive the blockers, publish the preview, create a V3 tag/Release, upload package repositories or merge an unverified candidate to main. A changed candidate SHA requires new exact-candidate evidence.
