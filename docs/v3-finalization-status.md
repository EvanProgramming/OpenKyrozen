# V3 finalization status

V3 is **not release-ready**. This is an implementation and investigation handoff, not a release manifest. No final-candidate gate bundle, V3 tag or Release has been created by this work.

## Implemented safeguards

- Matched evaluation now retains independent scope, original-test integrity, provider, process and worker-completeness evidence. Missing evidence fails closed. The first safety failure is checkpointed and stops the campaign; resuming cannot skip it. Malformed worker JSON is preserved as an incomplete result rather than losing the checkpoint.
- DeepSeek has immutable CNY pricing, a cumulative15CNY cap, serial requests and fixed2.032768CNY worst-case reservations. Original source ledgers are read-only, copies deduplicate, and proven carried history expands to original attempt records. Smoke, retries and cohort share the ledger; unknown use stays reserved.
- A two-request native-tool smoke checks unpredictable arguments, correlated function-call output, final response and complete usage before the unchanged180-row cohort.
- Original fixture tests are OS-protected for both benchmark arms. The benchmark-only external shell broker enforces parent-owned bounds and request-bound tickets. Real macOS tests cover legitimate writes, protected-file attacks, narrow scope, output/exit/cancellation/timeout and frozen V2 compatibility.
- Public README, installation guide and website commands remain on the published v2.0.8 assets. Candidate package metadata remains3.0.0. V3 publication must precede switching public download instructions.

## Release blockers

1. **Actor authentication:** runtime subagents share the Python worker process. An inherited root control channel does not prove a request's narrower child identity. Context variables or worker-supplied approval/database state are not independent authorization. Trusted actor creation/dispatch with isolated actor channels is still required.
2. **Detached macOS descendants:** a command can fork, call setsid, outlive its original parent and close its output. Current ancestry/process-group cleanup cannot reliably find that detached orphan. The regression characterization explicitly kills its escaped test child; it does not certify cleanup. Local SDK headers document that the old kqueue NOTE_TRACK family is unsupported. A supported ownership boundary must be established before the lifecycle gate can pass.
3. **Linux evidence:** the local Colima VM rejects Bubblewrap UID mapping. Structural mount-order tests are not Linux kernel enforcement evidence; actual supported-runner tests remain necessary.
4. **R260 history:** the original failure was a five-second future-completion timeout after cancellation. New instrumented Windows3.12/3.13 runs completed50 iterations each against both the old failing product and prior candidate (200 total) without reproducing the timeout. This does not establish the original cause. R260 remains inconclusive.
5. **Final evidence:** requirement-by-requirement acceptance, final six-platform/30-minute validation, the live180-row comparison, all17 signed passing records and the same-SHA main integration remain incomplete.

The full campaign preflight deliberately rejects the unproven actor/descendant boundary before credential lookup or model requests. The component probe's limited coverage must never be promoted to a full passing gate or used to bypass that rejection.

## Evidence and constraints

- Approved execution plan: [V3 finalization](superpowers/plans/2026-11-07-v3-finalization.md).
- Windows candidate diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261579117
- Windows historical diagnostic: https://github.com/EvanProgramming/OpenKyrozen/actions/runs/42261682019
- Detailed local execution logs,266 requirement/comment inventory and historical Windows artifacts are outside the checkout in `/Users/evangong/.codex/worktrees/openkyrozen-v3-evidence/`.
- Original DeepSeek ledgers independently reconstruct settled0.58291496CNY plus held0.000000000007CNY, total0.582914960007CNY. This work has made no external DeepSeek requests.

Do not waive the blockers, publish the preview, create a V3 tag/Release, upload package repositories or merge an unverified candidate to main. A changed candidate SHA requires new exact-candidate evidence.
