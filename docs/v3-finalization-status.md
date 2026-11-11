# V3 finalization status

V3 is **unpublished**. This document describes implemented safeguards, retained investigation results and required acceptance evidence. Release readiness is established by the external, signed R250–R266 bundle for the exact clean candidate; source changes and intermediate passing tests do not establish it. The final workflow stops before creating a V3 tag or Release.

## Implemented safeguards

- Matched evaluation now retains independent scope, original-test integrity, provider, process and worker-completeness evidence. Missing evidence fails closed. The first safety failure is checkpointed and stops the campaign; resuming cannot skip it. Malformed worker JSON is preserved as an incomplete result rather than losing the checkpoint.
- Worker answers, receipts, task state and plan events require durable, parent-bound artifacts for independent review after workspace cleanup. Resume and completeness checks reject missing, modified or cross-row evidence. Protection probes use isolated temporary homes and a minimal environment before any provider credential lookup.
- DeepSeek has immutable CNY pricing, a cumulative 15 CNY cap, serial requests and fixed 2.032768 CNY worst-case reservations. Original source ledgers are read-only, copies deduplicate, and proven carried history expands to original attempt records. Smoke, retries and cohort share the ledger; unknown use stays reserved.
- New and missing DeepSeek model slots both default to `deepseek-flash`; explicit saved model choices remain effective. Equal default slots do not demonstrate a model-selection benefit.
- DeepSeek's two-request native-tool smoke checks unpredictable arguments, correlated function-call output, final response and complete usage before the 180-row cohort layout. Gemini and native Panstar Auto check the same native continuation in each role slot, four requests in total.
- Each guarded provider then runs one protected V3 planning preflight: create and explicitly start a task, read a fixed file against its typed acceptance predicate, confirm its completed state and finish the response. Real native receipt identities, durable task criteria and immutable worker evidence must agree. Write/command/external-effect receipts fail the preflight even if files are later restored. At most six planning-preflight requests may reach the metered upstream; this limit does not change formal fixture execution. Before declaring success, the parent closes phase admission under the reservation lock and validates a snapshot containing all already-admitted requests. Later old-phase requests are refused even below the request limit; unsettled or extra calls cannot certify completion.
- Original fixture tests are OS-protected for both benchmark arms. The benchmark-only external shell broker enforces parent-owned bounds and request-bound tickets. Real macOS tests cover legitimate writes, protected-file attacks, narrow scope, output/exit/cancellation/timeout and frozen V2 compatibility.
- Process lifecycle audit persists scoped launch intent before dispatch and retains uncertain outcomes across restart. Historical PIDs are observation metadata, never authority to reattach, signal or replay. Audit failure cannot certify cleanup, and bounded audit writes preserve cancellation under SQLite contention.
- Learning promotion requires complete paired outcome, verification, cost, latency and regression measurements. Missing dimensions and old success-only replay records remain incomplete. Known failed effects replace unsupported final success claims with receipt-derived results, including valid structured child reports.
- Public README, installation guide and website commands remain on the published v2.0.8 assets. Candidate package metadata remains 3.0.0. V3 publication must precede switching public download instructions.

The current formal provider policy is PanstarCloud GPT with the built-in automatic selector: simple `gpt-6-sol`, complex `gpt-6.1-sol`, no main-model pin, native Responses and paid upper-rate accounting at 8 CNY/USD. The owner removed the cumulative ceiling and still prohibits publication. Each request reserves CNY 2.80068096 for the full financial input/output bound; actual selected/served identity and consistent native usage determine settlement. All earlier provider costs and unknown holds remain included. Four native tool-continuation smoke calls and the actual planning preflight must pass before the unchanged 180 → 54 → 36 cohorts. The new policy requires a newly frozen signed candidate and complete fresh offline evidence; historical passing candidates and stopped Gemini attempts cannot certify it. See [evaluation policy](v3-evaluation.md#panstarcloud-automatic-selection-campaign).

Model-routing acceptance also requires the separate 36-row Auto-versus-configured-simple contrast after the original 54-row supplement. Its ordered physical-call exposure and measured quality/latency evidence remain unproven until executed on the final candidate; distinct configured slot names alone cannot establish a routing benefit. The three phases share one linear ledger history and inherit the same finite or explicitly unlimited accounting policy.

## Owner acceptance amendment, 2026-11-11 (Asia/Shanghai)

The retired historical macOS incident is no longer a release blocker: the owner
explicitly waived its retrospective attribution and requested removal of the
canonical local failed soak record. No further incident-specific capture or
repetition is required. The external ledger records the scope/removal rather
than relabeling the old run as passed. This amendment supersedes older status
statements about that incident only. Current-candidate six-platform and
30-minute acceptance, Windows reliability, isolation, provider evaluation and
all remaining release requirements still need their own evidence.

## Required release evidence

1. **Final actor/platform evidence:** fresh-process delegation, kernel UID ownership, immutable parent policy, read-scope protection and UID-based detached-process cleanup are implemented. All four Linux/macOS Python combinations passed the privileged suite for intermediate commit `c87ecef`; the earlier six-platform workflow also completed all six 30-minute soaks. Both results remain historical after source changes. The release bundle must establish all of these checks for the final clean candidate.
2. **R260 history and causal closure:** native Windows file-I/O evidence and controlled regressions now explain the owned-connection last-close mechanism at the unchanged five-second cancellation deadline. A separate historical readonly failure has an observed same-process live-database snapshot/sharing collision; a real three-arm CopyFile2 experiment reproduces SQLite READONLY and verifies the active-database exclusion. Independent review and owner adoption qualify R260 for intermediate signed `842588d`, using its fresh complete six-cell, platform, isolation and local evidence. Original failed captures and earlier BLOCKED reports remain unchanged. The first historical exception's exact connection/extended code and original unique trigger remain unknown; no unique historical attribution is claimed. This is a mechanism-level causal closure, not an acceptance transfer to later source changes. The final candidate still requires its own complete evidence.
3. **Complete acceptance:** the release bundle must establish default-on routing benefit, requirement-by-requirement acceptance, final six-platform/30-minute validation, the live 180-row comparison and all 17 signed passing records. Main must then point to that same verified SHA without rewriting it.

The full campaign requires a privileged disposable runner and executes real actor and descendant checks before credential lookup or model requests. The optional bounded component probe returns explicitly incomplete isolation evidence and cannot replace that preflight.

For a planning-only readiness check, append `--planning-preflight-only` to the existing guarded `python -m benchmarks.v3_deepseek` invocation. Preserve its exact signed source, output, budget database and original prior-ledger arguments. Success exits zero with `planning_preflight_passed`, while formal rows and release-completeness flags remain empty/incomplete. Remove only that flag to continue the same campaign: before credential lookup, resume rechecks the candidate/apparatus, fixed fixture and rendered prompt, exact settled call set and usage, contained worker evidence and actual verified completion. Failed or interrupted preflights stop and cannot automatically retry or start the cohort. Smoke, preflight and formal work share the cumulative 15 CNY ledger and unchanged 2.032768 CNY per-request reservation; all unknown holds remain. The preflight contributes no row to the formal 180 or separate 54, and establishes no model-quality or release gate.

## Functional acceptance audit

The 266 source criteria were independently audited against implementation and regression bodies: 196 implemented-with-regression, 52 partial and 18 unproven at the audit baseline. Partial includes both concrete functional gaps and missing acceptance evidence. Those baseline counts are not a final acceptance result. Issue closure alone is not completion.

Implemented repairs cover unsuccessful provider finishes, native streaming, explicit task start/completion, immutable strategic objective anchors with scoped refinement, pinned Working Plans, unknown-effect restart protection, turn-scoped background cancellation, advisory-only Jev decisions and evidence-gated learning promotion. Strategic transitions now belong to `tasks.planning.PlanningService`; the interaction controller retains mode and question adaptation.

Native Agent, Ask and Plan paths use typed execution/control contracts. Plan proposals preserve explicit user acceptance, material revisions pause mutations, and safe narrowing preserves the existing automatic-acceptance policy. Lazy legacy helpers and reply-specific provenance keep literal native text intact through CLI/Web/TUI formatting and delegated synthesis. Complex read-only children can edit only their own internal plans and retrieve only their own tool artifacts, without receiving filesystem write authority.

Native context preflight now includes tool schema and continuation payload estimates, binds reported counts to provider/model/schema identity, and compacts old complete history units without splitting call/result groups. Overflow recovery retries only generation, once, before any emitted stream content; it cannot replay an executed tool. Prose-only task criteria remain pending unless trusted verification exists: a successful file write cannot certify an unrelated test or business outcome.

A premature final response with an unfinished Working Plan, or a missing required plan, receives one bounded structured continuation per turn. Both cases share that allowance; authority, cancellation and unknown effects still stop execution. The continuation refers to the pinned plan without repeating unbounded task criteria. Google and Vertex output usage includes thinking tokens, including cumulative streaming snapshots, without charging snapshots twice. Google product cost estimates use model-specific dated Standard rates and cache counts, preserving requested and served identity. Missing or invalid counters and unknown models retain unknown cost; summaries exclude unknown monetary amounts while retaining their coverage counts. These product estimates remain separate from the benchmark parent's cumulative budget. Focused and independent checks precede final-candidate acceptance.

The repaired 30-fixture cohort and observer/verifier identities are frozen before any paid run. Multi-file/API, refactor, Git and multi-turn context oracles retain the original comparison thresholds and shared physical success criteria. New regression, packaging and platform evidence must bind the eventual clean signed candidate; intermediate passing suites cannot be combined into a final release manifest.

## Evidence and constraints

- Approved execution plan: [V3 finalization](superpowers/plans/2026-11-07-v3-finalization.md).
- Windows candidate diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261579117
- Windows historical diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261682019
- Historical Windows load controls: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42288273719
- Intermediate four-platform privileged isolation: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42349253524
- Incomplete continuous Windows native capture: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42350403044
- Detailed local execution logs, 266 requirement/comment inventory and historical Windows artifacts are outside the checkout in `/Users/evangong/.codex/worktrees/openkyrozen-v3-evidence/`.
- The initial offline audit reconstructed settled 0.58291496 CNY plus held 0.000000000007 CNY, total 0.582914960007 CNY. This is historical exposure, not a live cumulative balance. Subsequent smoke, retry and cohort charges, including every unknown reservation, must be reconstructed from the original ledgers in the external exact-candidate evidence.

Apply only the incident-specific owner amendment above; do not waive any remaining blocker, publish the preview, create a V3 tag/Release, upload package repositories or merge an unverified candidate to main. A changed candidate SHA requires new exact-candidate evidence.
