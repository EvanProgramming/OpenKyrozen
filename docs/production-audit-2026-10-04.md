# Final production audit — 4 October 2026

This is the historical HOLD snapshot. The [shipping follow-up](production-shipping-audit-2026-10-04.md) supersedes its unresolved gates with new evidence.

**Recommendation: HOLD.** The repaired candidate passes the available source, package, native platform, browser, and 30-minute workflow checks. Live delegated review did not finish within the authorized provider budget, and live in-flight cancellation was not reached. These critical acceptance checks remain unverified. Main publication and the subsequent published-main `/update` check are therefore withheld. No release was tagged or published.

## Candidate and evidence identity

The implementation candidate is GPG-signed commit `e408ae7b4417a521161e50c88ef2eb97717e78c8` on `codex/production-audit`. It contains self-learning commit `4949695a08e326e0b71833932c790a5589fa956f` merged with main baseline `94a1d99eab337719308cbf998b0515f454c1acc6`. The original checkout's contributor documentation and runtime files were preserved outside the isolated candidate.

[Candidate PR #211](https://github.com/EvanProgramming/OpenKyrozen/pull/211) remains a draft. [All ten implementation CI jobs passed](https://github.com/EvanProgramming/OpenKyrozen/actions/runs/37141852478). Subsequent report and harness commits preserve the implementation identified above. The PR checks and their uploaded receipts identify the exact revision tested on each subsequent run. The final local and native Linux matrices passed all 442 tests on both supported Python versions.

[Machine-readable evidence](production-audit-evidence-2026-10-04.json) records immutable revisions, platform, interpreter, dependency versions, installed provenance, native update outcomes, request counts, and local log hashes. Local matrices used Python 3.12.13 and 3.13.14 on macOS 26.4 arm64, Go 1.27.1, ChromaDB 1.5.9, Playwright 1.63.0, FastAPI 0.142.2, and OpenAI SDK 3.24.0. Native runner identities and installed provenance are retained in the receipts. The native acceptance receipts include the dependency inventory, interpreter, prefix, and module source from the active installed generation.

## Repairs

- Updates resolve main once, prepare and validate the TUI for that revision, then install and verify Python before activation. A structured `success`, `partial`, or `failed` result controls restart readiness; message text no longer controls restart.
- A shared process lock covers update completion and launcher validation. A durable transaction manifest blocks activation after an incomplete installation. Fresh isolated interpreter probes check package provenance and entrypoint paths. The launcher selects the backend from its own Python environment and removes inherited Python path overrides.
- Windows prepares a permanent separate Python generation, preserving the running environment. A detached activation worker waits for exit, checks the generation token, replaces entrypoints/TUI under the shared lock, probes the recovery CLI, and restores prior files on failure. Successful activation removes stale legacy pending TUI files so they cannot overwrite the verified binary on relaunch. Until activation completes, the command truthfully reports a partial, verified staged update and requests manual exit/relaunch.
- The replacement interpreter installs Chromium for its Playwright SDK and verifies a real headless page before activation. Browser preparation failures report a partial update with recovery instructions and suppress restart. Native update checks use isolated browser caches.
- Historical v2.0.4 release wheels use legacy entrypoints. Verification accepts those installed paths while retaining exact release URL/version checks and checksum/version verification for the legacy TUI.
- Matching learning products remain eligible through scoped SQLite retrieval when the derived vector index omits them or ranks source observations first. Visibility, profile, ownership, relevance, and tool capability boundaries remain enforced.
- Delegation repairs conflicting Plan-only responses before creating sequential parent tasks. Gateway audit startup logs now use files rather than undrained pipes and include startup stack diagnostics.

Every confirmed correctness repair has a failing regression followed by a passing check. The independent review's serialization and provenance findings were addressed; its weak live cancellation oracle was replaced with an explicit in-flight request barrier. The strengthened live oracle has not yet run to completion.

## Feature matrix

PASS means the stated evidence passed; it does not imply every provider and platform combination was exercised live.

| Feature or gate | Status | Evidence and practical boundary |
|---|---|---|
| Python 3.12 and 3.13 production gates | PASS | Each exact Make matrix passed 442 tests with browser integrations enabled, plus syntax, inventory, and documentation checks. |
| Onboarding and provider contracts | PASS | Distribution, provider registry, provider adapter, startup, and rebuilt TUI acceptance. Optional SDKs are retained by updates. |
| Configured provider chat, streaming, learning reuse | PASS | Earlier bounded DeepSeek V4 Flash checks produced chat/stream results and a learned product/use receipt. Their revisions are preserved in the evidence; final local/CI regressions cover subsequent recall repairs. |
| Other providers and mixed-provider live flows | BLOCKED | Credentials were unavailable. SDK/contracts passing is not live-provider acceptance. |
| Tool execution, permissions, interaction modes, approvals | PASS | Permission, interaction, workspace, streaming, and runtime suites; real TUI approval and interaction flows. |
| Agent and delegated review with deterministic providers | PASS | Agent/sub-agent acceptance, concurrent ownership, reviewer receipts, cancellation, recovery, and Plan-only regression. |
| Live delegated review and in-flight cancellation | BLOCKED | 23 + 27 + 20 actual provider requests used with the audit complex model unified to Flash. Production complex-model combinations remain unverified. The final 20-request run stopped at the budget guard before review completed; cancellation was not reached. |
| Durable tasks, history/rollback, memory isolation | PASS | Real effects, collision/scope tests, history regressions, native upgrade state preservation, browser restart recovery, and soak record integrity. |
| All twenty learning switches and worker lifecycle | PASS | Dispatcher/worker suites cover independent flags, products/use receipts, persisted preferences, scoped retrieval, failure isolation, and restart behavior. |
| Skills, plugins, Graphify, GitHub integration | PASS | Registry, plugin, private project graph, and GitHub CLI suites. Real updater GitHub CLI setup passed as an optional component. External account operations were not performed. |
| Scheduling, usage accounting, webhooks | PASS | Scheduler, usage-ledger reconstruction/cache accounting, webhook boundary/completion suites, and repeated durable-task scheduling. |
| Installed web UI | PASS | Installed site-packages provenance; real browser authentication, invalid-token handling, SSE rendering, session isolation, reload reconnect, and restart recovery after reauthentication. Provider transport was a deterministic local fixture. |
| TUI, wheel, and sdist | PASS | Go tests, rebuilt terminal acceptance, and installed package smoke outside the checkout. |
| macOS and Linux old-to-candidate/repeated update | PASS | Real uv installations upgraded from the pinned main baseline; Python/TUI agreed on the target revision; settings, chats, tasks, memories, and workspace files survived. Repeated updates prepared and launched Chromium using the installed SDK. |
| Windows installer/staged update activation | PASS | Native installer syntax and real candidate repeated staged activation, with matching Python/TUI and preserved state. Historical Windows updaters require an external installer recovery step before using the fixed updater; that transition is recorded explicitly. |
| Docker replacement persistence | PASS | Native Linux CI built/replaced containers and retained durable state. |
| Real release fallback | PASS | Actual v2.0.4 wheel plus checksum-verified TUI source; verified component outcome, restart readiness, and usable recovery CLI. This installs the known release when main cannot be resolved, rather than claiming latest-main provenance. |
| Thirty-minute stability workload | PASS | 1,800.5 seconds, 540 tasks, two concurrent sessions, 22 restarts, cancellation/worker regression cycles, SQLite integrity and uniqueness checks, no orphan audit children, peak gateway RSS 96.5 MiB within configured bounds. |
| Published-main update and release integration | BLOCKED | Main remains at the baseline because critical live acceptance is unverified. No main push, tag, or release publication was performed. |

Earlier soak attempts hit readiness timeouts; one confirmed harness backpressure risk was repaired, while the empty-log timeout's exact cause was not proven. The completed run gives bounded evidence for the stated workload, not an unlimited stability guarantee. Its gateway/memory source was the working candidate subsequently included in `d600279`; the later fallback, launcher, and Windows activation changes do not alter those exercised paths.

## Reproduction

Use Python 3.12 or 3.13 and the repository's normal browser/vector integrations. Source tests must have permission to bind local sockets.

```sh
make test check lint docs-check
# Repeat with PYTHON and VENV_PYTHON both pointing to the Python 3.13 environment.
cd tui && go test ./...
```

Build/install outside the checkout, then run acceptance with the installed interpreter's **unresolved virtualenv path**:

```sh
python scripts/wheel_smoke.py
python scripts/agent_workflow_acceptance.py
python scripts/subagent_workflow_acceptance.py
python scripts/tui_workflow_acceptance.py /absolute/path/to/rebuilt/openkyrozen-tui
python scripts/installed_web_acceptance.py --python /absolute/path/to/installed/bin/python --output /tmp/installed-ui.json
python scripts/production_soak.py --seconds 1800 --output /tmp/production-soak.json
python scripts/update_workflow_acceptance.py --revision e408ae7b4417a521161e50c88ef2eb97717e78c8 --baseline git+https://github.com/EvanProgramming/OpenKyrozen.git@94a1d99eab337719308cbf998b0515f454c1acc6 --output /tmp/update-acceptance.json
```

The exact prior live attempts and their counters are in the evidence JSON. Do not rerun live acceptance without a newly authorized request budget. The saved synthetic audit harness supports `--focused` to restrict future checks to delegated review and the in-flight cancellation barrier.

## Remaining release gate

Obtain complete bounded live delegated-review and in-flight cancellation evidence. The harness now preserves the configured simple/complex models and records bounded response metadata without prompts, responses, or credentials. Capture those protocol diagnostics on the next attempt so a budget stop can be distinguished from a model/runtime loop. Reconfirm remote main, integrate without force-pushing only when critical gates pass, verify CI, then run a real update resolved from published main and confirm matching Python/TUI plus restart. Release tagging/publication remains outside this audit.
