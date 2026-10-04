# Production shipping follow-up — 4 October 2026

This report supersedes the earlier HOLD recommendation for the gates rechecked below. The original report and evidence remain a historical record of the unfinished 70-request audit.

**Recommendation: SHIP the tested configuration.** The pre-merge stability gate passed and PR #211 was merged through GPG-verified `5289399e2c722b21bffbc9c31a3c3fb0abe924e1`, whose tree matches the tested candidate. No tag or release is created by this audit.

The draft PR was intentional: independent delegated review and genuine in-flight provider cancellation had not completed under the previous request budget. The new user authorization permits merging after verification and up to 5 RMB in additional live testing. The repaired source and installed published-main workflows now pass both critical live gates with the user's configured Flash/simple and Pro/complex models.

Implementation candidate: signed `6bc93b0cb5f3e35be28e4b451699e5080018e41c`; published signed merge: `5289399e2c722b21bffbc9c31a3c3fb0abe924e1`. [Candidate CI](https://github.com/EvanProgramming/OpenKyrozen/actions/runs/37175300096) and [main CI](https://github.com/EvanProgramming/OpenKyrozen/actions/runs/37176860677) both passed all ten jobs. [Machine-readable evidence](production-shipping-evidence-2026-10-04.json) retains platforms, dependencies, interpreter/package provenance, revisions, budget reservations, failures and acceptance receipts. Later report/harness-only commits do not change production runtime; each native CI receipt identifies its own installed target revision.

Local source matrices used Python 3.12.13 and 3.13.14 on macOS 26.4 arm64. Native Linux, macOS and Windows identities and complete installed dependency inventories are in the receipts. Original local documentation and runtime files remain outside the isolated candidate.

## Additional repairs

- Recompute delegation classification after each planning retry. A retry that changes from direct tools to `spawn_agents` no longer creates an unfinishable parent task.
- Exclude `.kyrozen` and the declared private graph-cache root from both Git and filesystem source inventories. Repeated indexing with a home/cache nested inside the workspace no longer recursively copies its own derived source trees.
- Use current Flash aliases and the September 10 price transition while preserving historical August rates. Apply published Chinese holiday discounts, mark unknown future holiday calendars as estimated, and attribute streaming usage to the returned model. Sources: [current DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/), [effective date](https://api-docs.deepseek.com/news/news260910/), and [official 2026 holiday calendar](https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm).
- Add a persistent monetary reservation guard to the synthetic live harness. Cancellation happens after response headers confirm a real provider request was sent; unknown cancellation usage keeps its full reservation. Natural-language delegation specifies exactly two source investigations and independent reviews; all source-read, task-completion, review, and final-report assertions remain enabled.

Each confirmed production defect was reproduced with a failing regression before repair. Browser acceptance fixtures suppress incidental detached learning workers; worker lifecycle is exercised separately.

## Live budget and interpretation

The first new batch found the planning-retry defect. A later batch stopped at the monetary guard, and another completed two reviewed investigations but failed the strict oracle after the model requested an extra assignment. Those attempts remain recorded, rather than being counted as passes. The final bounded two-investigation workload and a separate cancellation check passed.

The 5 RMB authorization is separate from the earlier exhausted request allowance. All new requests share one durable ledger. Verified Sunday usage was reconciled at the documented off-peak Pro cache-miss rates, which bound both configured models; cache-hit discounts are deliberately ignored. Unknown cancellation usage retains the worst-case input/output reservation. Final cumulative upper bound: **4.4829315 RMB across 150 new requests**, including full reservations for three cancellations with unknown usage. This is below the user's 5 RMB limit. Cost figures are conservative upper bounds, not an account invoice. No credentials or private chats are included. Provider diagnostics contain bounded structural metadata and synthetic task outcomes.

## Final gates

| Gate | Status | Evidence boundary |
|---|---|---|
| Python 3.12/3.13, browser/vector enabled | PASS | Full Make test/check/lint/docs-check matrices, 453 tests per version. Python 3.12 passed on serial rerun after a parallel two-second SSE timeout; the test was unchanged. |
| Go/TUI, installed wheel/sdist, Docker | PASS | All ten candidate and main CI jobs, rebuilt TUI, clean wheel/sdist, Docker replacement persistence, and installed acceptance. |
| macOS/Linux/Windows update and persistence | PASS | Native real old-to-candidate/repeated upgrades; Windows historical updater recovery remains explicit. |
| Configured live chat/stream/learning reuse | PASS | New bounded source run; provider credentials unavailable for other providers. |
| Configured live delegation/review/cancellation | PASS | Source and installed published-main workflows; real delegated tools, independent source reviews, parent task completion, final report, and response-header cancellation. |
| Installed browser and Graphify | PASS | Real installed wheel, authenticated UI/restart/session checks, and repeated actual Graphify with nested home/cache and no provider calls. |
| Thirty-minute concurrent stability | PASS | 1,800.34 seconds, 552 tasks, 23 restarts, peak gateway RSS 97.4 MiB, startup range 0.844–8.469 seconds. No lost/duplicated records or orphan audit processes. Declared startup allowance 60 seconds; the stricter failed attempt remains recorded. |
| Published-main `/update` and restart identity | PASS | Real main resolver from legacy baseline, repaired repeated update, matching installed Python/TUI, recovery CLI and preserved state. Entering `/update` in the installed TUI automatically replaced both TUI and backend processes; the published-mode checker changes neither backend nor resolver. |
| Other providers live | BLOCKED | No credentials; source adapter checks do not establish live acceptance. |

The installed TUI canonical `/update` also passed on the pinned candidate: external process IDs prove both the TUI and backend were replaced automatically, matching installed provenance/TUI revisions were verified, and all seeded state survived. The pinned candidate check uses a temporary console shim only to select the immutable resolver target; published-main mode leaves the installed backend and resolver unchanged.

The first fresh stability attempt failed at restart 19 after 1,439.6 seconds: the 20-second test readiness deadline expired while Python was still importing modules. Its 456 tasks all succeeded and the preserved database passed integrity checks. Eight immediate follow-up starts took 2.1–12.5 seconds. Host memory pressure/swapping may contribute, but the exact cause is not proven. The repeated full workload uses a declared 60-second startup allowance and records every startup duration; its state, resource, cancellation, and orphan checks are unchanged. This is a harness acceptance adjustment, not a claim that production code repaired the import delay.

The earlier feature matrix continues to cover onboarding, permissions, approvals, modes, durable tasks, rollback, memory isolation, all learning flags, skills/plugins, GitHub integration, scheduling, usage accounting, and webhooks. These suites are included again in the final full matrix. External account mutations and unavailable-provider combinations remain unverified.

## Reproduction

Run `make test check lint docs-check` with both `PYTHON` and `VENV_PYTHON` set to the supported interpreter. Run Go/TUI and distribution acceptance through native CI. Use normal browser/vector integrations and permissions for local sockets.

For a real published-main upgrade outside the repository:

```sh
python scripts/update_workflow_acceptance.py --published-main --revision PUBLISHED_SHA --baseline git+https://github.com/EvanProgramming/OpenKyrozen.git@94a1d99eab337719308cbf998b0515f454c1acc6 --output /tmp/published-main-update.json
```

For the real installed TUI command and automatic restart:

```sh
python scripts/installed_tui_update_acceptance.py --revision PUBLISHED_SHA --source-root /absolute/path/to/current-checkout --output /tmp/published-main-tui.json
```

For installed live checks, use the installed interpreter's unresolved virtualenv path, `-I`, `KYROZEN_ACCEPTANCE_INSTALLED=1`, `--focused`, and the existing `--budget-ledger`. A paid rerun needs remaining user-authorized monetary budget. Run `scripts/production_soak.py --seconds 1800` for the repeated two-session workload.

“Always updates” means verified latest-main updates when dependencies/network are available, and truthful failed/partial outcomes with a usable recovery path otherwise. The historical first upgrade uses its legacy result; the repaired repeated update verifies both build identities. Historical Windows updaters require external installer recovery before using the new staged activation path.

The finite tests support shipping the tested configuration; they do not guarantee every future provider response or outage. Release tagging/publication remains outside this audit.
