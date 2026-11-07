# V3 finalization execution plan

Approved by the user on 2026-11-07 in the V3 finalization chat.

## Binding requirements

Audit all 266 source requirements in milestone issues #227–#268 and the #319 regression, including issue comments and dependent PRs. Require all seventeen R250–R266 full-release gates to pass against one clean signed candidate. No preview waiver qualifies. Preserve Python 3.12/3.13 and the documented Windows supported-feature exceptions.

## Work sequence

1. Start at stable candidate 7905fb634fe447243668c2d01179ce5cb2cdb275 in an independent checkout outside the synchronized Documents directory; preserve original runtime data and evidence. Selectively reuse diagnostic protection tests; never merge the diagnostic branch's reduced CI.
2. Retain separate scope, original-test, provider, process and worker-completeness evidence. Missing evidence fails closed; persist before stopping on the first safety failure. A resumed campaign cannot skip a recorded safety failure.
3. Protect original fixture tests and ancestors identically in both benchmark arms. Implement the approved parent-authorized external shell launcher, preserving sandbox boundaries and process lifecycle. The parent independently enforces frozen fixture/accepted-plan authority, monotonic narrowing and child intersections. Single-use tickets bind identities, full command/cwd/environment and policy revision. Reject forged approval, replay and stale/cross-session requests. Prove direct and managed-shell attacks fail on real Linux/macOS while legitimate source edits work. No supported sandbox means no credentials or provider requests.
4. Explain the historical Windows R260 cancellation failure with causal evidence and a reproducing regression, or retain the blocker. Collect exact dispatch/provider/cancellation/cleanup timing, four-second thread dumps and database/process state. More successful repeats or larger timeouts are insufficient.
5. Move DeepSeek configuration into immutable in-tree pricing/relay configuration. Reconstruct cumulative exposure from original ledgers, deduplicate copies, preserve unknown reservations, cap total at 15 CNY. Use deepseek-flash Responses consistently for both arms, output 4096; reserve 1M input plus output at peak prices, 2.032768 CNY per attempt. Smoke calls and retries count. No calls before offline blockers pass.
6. Run full tests/check/lint/docs/architecture, Go, wheel/sdist external installs, CLI/Web/TUI/update and Docker persistence. Six OS/Python combinations require workflow acceptance and >=1800s soak; record Windows POSIX exceptions honestly.
7. Freeze the final signed SHA, then run the unchanged 30 fixtures x 3 repeats x 2 arms. Require complete accounting, paired-success 95% CI lower bound >0, request and token savings >=10%, V3 recovery count zero and all independent V3 acceptance/planning checks passed. No cross-candidate row reuse, cherry-picking or lowered thresholds.
8. Create signed exact-SHA gate records/manifest and committed-tree distribution artifacts outside the checkout. Review; fast-forward main non-forcibly to the same fully verified SHA after rechecking rules and concurrent changes. Re-freeze if identity changes. Public downloads stay on the actually published stable release until V3 publication.

## Stop boundary

Do not create tags, draft/public Releases, upload package repositories, or invoke the preview publisher. Missing evidence, budget exhaustion, provider outage or an unexplained R260 failure remains a blocker; do not call the work release-ready.

## Execution evidence

External execution ledger and unmodified source requirement/comment snapshots: `/Users/evangong/.codex/worktrees/openkyrozen-v3-evidence/`. These are progress records, not passing release authorization. Credentials must never enter tracked files, reports or logs.
