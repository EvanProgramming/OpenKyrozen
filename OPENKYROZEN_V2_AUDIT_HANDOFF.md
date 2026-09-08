# OpenKyrozen V2 Audit: Complete Remediation Handoff

## Purpose

This document is the implementation handoff for every confirmed defect found during the macOS and DeepSeek V4 Flash behavior-complete audit of OpenKyrozen V2.

The audit was performed against `main` at commit `6b2eaa2980c5f7e1ea313b5aa378818db8f7c349`. It used a clean GitHub clone, isolated HOME/workspaces/databases/vector indexes/browser profiles, the documented installation paths, Python 3.12, a live `deepseek-v4-flash` provider, a real Chrome UI, local Git repositories, and a local bare remote.

No product fixes were made during the audit. The resolver should address all issues below, add regression coverage, update affected documentation, and re-run the complete acceptance matrix before closing anything.

## Result summary

| Priority | Count | Issues |
|---|---:|---|
| P1 | 5 | #73, #74, #77, #80, #83 |
| P2 | 8 | #75, #76, #78, #79, #81, #84, #85, #86 |
| P3 | 1 | #82 |
| Total | 14 | #73–#86 |

All issues were reproduced, deduplicated against open and closed GitHub issues, filed or reused, and read back after creation. Issue #80 is a confirmed regression of the duplicate-work risk previously tracked in closed issue #19.

## Rules for the remediation agent

1. Work from current `main`; first verify whether any issue has already changed since `6b2eaa2`.
2. Fix root causes in shared paths, not only the exact reproduction examples.
3. Preserve capability, authentication, path-safety, private-memory, and approval boundaries.
4. Never put real credentials, private paths, memory contents, or user data in tests, logs, commits, or issues.
5. Use temporary HOME, workspace, SQLite, vector, browser, and Git fixtures for tests.
6. Add a focused regression test for every issue. Tests must fail on the audited commit and pass after the fix.
7. Keep source/build, runtime, browser, and documentation evidence distinct.
8. Do not close an issue based only on code review or unit tests. Re-run its real acceptance flow.
9. After all fixes, run the full validation checklist at the end of this document.

## Recommended implementation order

The order below minimizes rework:

1. **Installation and test baseline:** #73, #75.
2. **Execution correctness and safety:** #74, #80, #83.
3. **One coherent usage-accounting design:** #77, #78, #79, #84, #85.
4. **Real provider streaming:** #86, coordinated with #77.
5. **Web/MCP integration and UI:** #76, #81, #82.
6. **Full clean-install, runtime, browser, restart, and documentation acceptance.**

The cost issues should not receive five unrelated patches. Introduce one precise usage record and aggregation path that supports provider/model identity, prompt cache hits/misses, input/output/reasoning tokens, price window, latency, streaming completion state, and durable reconstruction.

The session issues should also share one safe session initializer used by REST, SSE, MCP, session recovery, and the browser UI.

---

## #73 — P1: README one-line installer cannot install the package

Issue: [#73](https://github.com/EvanProgramming/OpenKyrozen/issues/73)

### Observed behavior

Running the exact documented macOS/Linux command in a fresh temporary HOME downloads `install.sh`, but `uv` cannot resolve `openkyrozen[web]` because no such PyPI release exists. PyPI returned HTTP 404 for the project.

```bash
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/main/install.sh | sh
```

The installer therefore never reaches its `kyrozen --version` and `kyrozen --help` verification steps. The primary new-user path is completely blocked.

### Root cause and affected components

- `install.sh` installs `openkyrozen[web]` from a package index.
- `install.ps1` is expected to use the same unavailable package.
- README files advertise both the one-line installer and direct PyPI installation.
- Project packaging may build locally, but no matching public distribution is available.

### Required outcome

Choose and implement one truthful distribution path:

- publish the correct package and extras to PyPI; or
- install an immutable, documented GitHub release/tag artifact with clear provenance.

Do not make the installer silently track mutable `main` unless that is an explicit documented policy.

### Acceptance tests

- Run the exact README command in a clean temporary HOME on macOS.
- Verify `kyrozen --version`, `kyrozen --help`, and `kyrozen-web --help` exit successfully from an unrelated directory.
- Verify installed version and source provenance.
- Verify update behavior from the installed package.
- Exercise the equivalent Windows installer in Windows CI or a real Windows environment.
- Update every README language and installation example to match the shipped source.

---

## #74 — P1: Failed durable shell tasks are recorded as succeeded

Issue: [#74](https://github.com/EvanProgramming/OpenKyrozen/issues/74)

### Observed behavior

A durable Web task running `run_cmd` with `false` produced `Exit code 1`, but the stored task status became `succeeded`. Its evidence also reported `success: true` and satisfied the acceptance condition.

### Root cause and affected components

The Web task executor in `server.py` derives success by checking whether result text begins with `Error:`. `run_cmd` reports non-zero termination as ordinary text containing `Exit code N`, so a failed command crosses the task boundary as success.

Relevant paths include:

- `server._execute_durable_task`
- `task_engine.TaskWorker`
- `tools.run_cmd`
- `tools.execute_terminal_command`
- task evidence and acceptance receipts
- task recovery after restart

### Required outcome

Replace textual success inference with a structured tool result or a shared authoritative success predicate. Exit code zero must be distinguishable from non-zero termination without parsing presentation text at each caller.

### Acceptance tests

- A zero-exit command becomes `succeeded` with successful evidence.
- A non-zero command becomes `failed` or `blocked`, with `success: false`.
- A timeout, signal termination, missing executable, and safety denial all remain failures.
- A failed command cannot satisfy an acceptance requirement.
- Restart/recovery preserves the correct failed state and does not reclassify it.
- REST task creation and scheduler-driven task execution use the same semantics.

---

## #75 — P2: Documented development installation cannot run the full suite

Issue: [#75](https://github.com/EvanProgramming/OpenKyrozen/issues/75)

### Observed behavior

After the documented `make install`, the repository test command did not have the Web dependencies needed to collect/run the complete suite. Installing `.[all]` exposed all 146 tests, but the real browser-flow test still failed because `all` omitted the existing `browser` extra and no Chromium binary was installed.

### Root cause and affected components

- `Makefile` installs only `requirements.txt` for development.
- `pyproject.toml` defines optional extras, but `all` does not include Playwright/browser support.
- The full test suite includes a real browser test.
- The development instructions do not establish a reproducible full-test dependency path.

### Required outcome

Define a truthful split between core development, full development, and optional real-browser acceptance. `all` must either mean all supported integrations or be renamed/documented more narrowly.

### Acceptance tests

- A fresh clone following the documented full-development commands discovers and passes all 146 or more current tests.
- Browser package installation and Chromium installation are explicit and automated where required.
- Environments intentionally lacking a browser receive a precise skip or a separate non-browser test target, not a misleading failure.
- CI covers both the lightweight path and the full browser path.
- `.[all]`, README files, Make targets, and CI agree.

---

## #76 — P2: `git_branch` is hidden from the default Web/MCP profile

Issue: [#76](https://github.com/EvanProgramming/OpenKyrozen/issues/76)

### Observed behavior

The runtime registers 31 tools and documents `git_branch` as a normal Git tool. Under the documented default `workspace` profile, MCP listed `git_status` but omitted `git_branch`. A direct call failed with JSON-RPC error `-32001` because the tool required `dynamic` capability.

### Root cause and affected components

`tools._TOOL_CAPABILITIES` has no `git_branch: git` mapping. Unknown tool names intentionally default to `dynamic`, so this single omission grants it the wrong capability classification.

### Required outcome

Classify `git_branch` consistently with the other Git mutations and regenerate all derived inventories.

### Acceptance tests

- `tool_capability("git_branch") == "git"`.
- Default `workspace` MCP discovery includes `git_branch`.
- A profile with Git capability can call it.
- A profile without Git capability cannot call it.
- CLI, Web, and MCP use the same mapping.
- Generated inventory and all README languages report `git`, not `dynamic`.

---

## #77 — P1: Streaming requests are absent from cost accounting

Issue: [#77](https://github.com/EvanProgramming/OpenKyrozen/issues/77)

### Observed behavior

A successful `OpenAICompatProvider.chat_stream()` request returned `STREAM_OK`, but the shared cost summary was byte-for-byte unchanged. The default Web UI uses the streaming route, so paid interactions may not move the visible cost badge at all.

### Root cause and affected components

`providers.OpenAICompatProvider.chat_stream()` collects emitted text but neither extracts final usage nor calls `_track_cost`. Equivalent behavior must be reviewed for all streaming-capable providers and fallback paths.

### Required outcome

Every billable request must produce exactly one usage record. Streaming must use authoritative provider usage when available and an explicitly labeled estimate only when it is not.

### Acceptance tests

- A completed stream updates input/output/cache/reasoning usage exactly once.
- A failed or interrupted stream records known billable usage without double counting.
- Provider fallback cannot charge the failed and successful legs as one ambiguous request.
- `/api/chat/stream`, `/api/cost`, the UI badge, CLI summary, and durable records agree.
- A browser test verifies visible cost changes after a successful stream.

---

## #78 — P2: Per-request cent truncation erases cumulative spend

Issue: [#78](https://github.com/EvanProgramming/OpenKyrozen/issues/78)

### Observed behavior

One thousand calls with 100 completion tokens each were reported as approximately `0c`, even though the combined 100,000 completion tokens have a non-zero cost under both the old table and current pricing.

### Root cause and affected components

`providers._track_cost()` converts each request to integer cents before aggregation:

```python
entry["cost_cents"] += int(request_cost_in_cents)
```

Every sub-cent request is rounded down independently.

### Required outcome

Store tokens and/or sub-cent decimal precision. Round only for presentation. Avoid binary floating-point for money where deterministic decimal or integer micro-units are sufficient.

### Acceptance tests

- 1,000 small requests equal one request with the same combined usage.
- Mixed prompt/output/cache categories aggregate correctly.
- Formatting shows useful precision below one cent.
- Reset, persistence, and concurrency do not lose increments.

---

## #79 — P2: DeepSeek rates do not match current V4 billing

Issue: [#79](https://github.com/EvanProgramming/OpenKyrozen/issues/79)

### Observed behavior

OpenKyrozen applies one legacy fixed pair—`$0.27/M` input and `$1.10/M` output—to every DeepSeek call. Current `deepseek-v4-flash` billing distinguishes model, cache hit/miss, and weekday peak/off-peak periods.

Official reference checked during the audit: <https://api-docs.deepseek.com/quick_start/pricing/>

### Root cause and affected components

- `providers.PROVIDER_COSTS` is provider-wide rather than model-specific.
- Usage parsing discards cache-hit and cache-miss token fields.
- No price-window timestamp/rule is recorded.
- The UI presents a single amount without explaining estimate uncertainty.
- README provider cost examples repeat the old input price.

### Required outcome

Create a model-specific pricing representation with deterministic effective-time and peak/off-peak handling. Persist the pricing inputs used for each calculation so historical totals do not change when a table is updated later.

### Acceptance tests

- Fixed-time tests cover peak and off-peak windows, including boundaries and time zones.
- Cache-hit, cache-miss, output, and reasoning-token behavior matches the provider contract.
- Unknown model/rate data produces an honest unavailable or bounded-estimate state.
- Documentation names the model and pricing date rather than presenting a timeless flat price.

---

## #80 — P1: CLI repeats successful side effects when `TaskDone` is omitted

Issue: [#80](https://github.com/EvanProgramming/OpenKyrozen/issues/80)

### Observed behavior

The real CLI was asked to create and verify a disposable file. `write_file` and `read_file` succeeded, but the task panel remained `0/3`. After roughly two minutes, OpenKyrozen executed the same successful `write_file` again and entered another long provider call. This was reproduced in two independent live DeepSeek turns.

### Root cause and affected components

- `_chat_turn_impl` relies on model-authored `TaskDone:` text to reconcile task progress.
- Successful tool receipts are not sufficient to complete matching planned tasks.
- A repeated action returned by the model is executed without duplicate-effect protection.
- Provider waits use a long SDK default without an OpenKyrozen-level bound.

This is a regression of the duplicate-work risk from closed issue #19.

### Required outcome

Tool execution receipts—not optional model formatting—must be authoritative for known effects. Repetition of an identical state-changing action in one turn must require an explicit reason or be rejected safely.

### Acceptance tests

- A provider that omits every `TaskDone` marker cannot cause an already-successful write to run twice.
- Planned tasks reconcile from successful receipts and acceptance evidence.
- Non-idempotent shell and Git operations receive duplicate protection.
- Legitimate retries remain possible after a verified failure.
- Provider calls have a bounded timeout/cancellation path and surface an honest blocked/error result.
- CLI task progress and final response agree after restart/recovery.

---

## #81 — P2: Web UI cannot resume or switch durable sessions

Issue: [#81](https://github.com/EvanProgramming/OpenKyrozen/issues/81)

### Observed behavior

Chrome successfully displayed a Unicode conversation. Reloading the page produced an empty chat and no session control, while `GET /api/v2/sessions/<old-id>` still returned the exact durable history.

### Root cause and affected components

The embedded UI in `server.py` creates a new ephemeral identifier on every page load:

```javascript
const sessionId = 'sess_' + Math.random().toString(36).slice(2,10);
```

There is no persistence for the active session ID and no accessible UI for listing, selecting, or loading the existing V2 sessions.

### Required outcome

Make the durable session API usable from the primary UI. Preserve the active session across reloads, support creating/selecting sessions, and restore the selected history.

### Acceptance tests

- Send Unicode messages, reload, and verify the same history is restored.
- Create two sessions and switch between them without content leakage.
- Restart the server with the same database and resume both sessions.
- Invalid or missing sessions produce a clear UI state.
- Session controls are keyboard accessible and properly labeled.
- Limit/prune behavior for the server's session cap is visible and deterministic.

---

## #82 — P3: Custom-port startup prints the wrong URL

Issue: [#82](https://github.com/EvanProgramming/OpenKyrozen/issues/82)

### Observed behavior

Launching with `--port 8876` printed OpenKyrozen's Ready URL as port 8000, followed by Uvicorn correctly reporting port 8876.

### Root cause and affected components

The server startup hook prints a literal `http://127.0.0.1:8000` rather than the effective CLI host and port.

### Required outcome

Report the effective endpoint exactly once, or omit the OpenKyrozen URL and rely on Uvicorn's authoritative message.

### Acceptance tests

- Default port output is correct.
- Custom host and port output is correct.
- Reload and non-reload launch modes do not conflict.
- IPv4, IPv6, and `0.0.0.0` display guidance is not misleading about the browser-reachable address.

---

## #83 — P1: Fresh MCP `chat/send` crashes before provider execution

Issue: [#83](https://github.com/EvanProgramming/OpenKyrozen/issues/83)

### Observed behavior

A fresh MCP `chat/send` session returned HTTP 500. The traceback ended in `_build_memory_context()` while attempting to create a set from `authorized_speakers=None`.

### Root cause and affected components

REST and SSE chat call `_set_memory_context(session, body)` before `_run_session_chat()`. The MCP branch only calls `_get_or_create_session()`, whose new-session dictionary omits `speaker`, `audience`, `channel`, and `authorized_speakers`.

The existing MCP test reused initialized state, masking the clean-session failure.

### Required outcome

Create one shared safe session initializer and use it for every surface. MCP internal failures must become structured JSON-RPC errors rather than framework HTTP 500 responses.

### Acceptance tests

- In a fresh process and empty database, make MCP `chat/send` the first request.
- Verify successful provider output and durable session history.
- Verify safe default memory actors match REST/SSE behavior.
- Verify invalid parameters use JSON-RPC error `-32602` or the appropriate protocol code.
- Verify unexpected internal failures return structured JSON-RPC errors without leaking sensitive details.
- Verify private-memory actor isolation through MCP.

---

## #84 — P2: Sub-agent learning records fabricate zero metrics

Issue: [#84](https://github.com/EvanProgramming/OpenKyrozen/issues/84)

### Observed behavior

A live researcher sub-agent returned `SUBAGENT_OK`, but its durable learning record contained:

```text
provider_model = unspecified
tokens = 0
latency = 0.0
```

### Root cause and affected components

`SubAgentManager.run()` starts a learning run without a provider/model and unconditionally completes it with `tokens=0` and `latency=0.0`. `_run_subagent_llm` returns result/tool data but does not propagate usage or elapsed time.

### Required outcome

Return a structured sub-agent execution result containing provider/model, usage, latency, result, receipts, and evidence. Unknown measurements must remain unknown rather than becoming factual zeros.

### Acceptance tests

- Mocked non-zero usage and latency survive through `SubAgentManager` into SQLite.
- A live DeepSeek run names `deepseek:deepseek-v4-flash`.
- Provider errors and fallback identify which attempts consumed usage.
- Tool-only or no-provider paths distinguish zero usage from unavailable usage.
- Learning metrics and `/api/cost` consume the same usage record.

---

## #85 — P2: Cost accounting resets across server restarts

Issue: [#85](https://github.com/EvanProgramming/OpenKyrozen/issues/85)

### Observed behavior

After live UI and REST calls, `/api/cost` reported about `5K in`. Restarting the server with the same project and SQLite database reset the endpoint to `No usage yet`, while durable run records still contained 5,510 tokens and session history remained intact.

### Root cause and affected components

The cost tracker is a module-global dictionary in `providers.py`. Usage is not durably stored by the accounting component and cannot be reconstructed at startup.

### Required outcome

Make cost accounting durable and define its reporting window. Installation-lifetime, workspace, session, and current-process totals should not be conflated.

### Acceptance tests

- Complete paid calls, restart, and verify the durable total is unchanged.
- Concurrent calls cannot lose increments.
- Stream, REST, MCP, CLI, and sub-agent usage enter the same ledger once.
- A deliberate reset is explicit, authorized, scoped, and auditable.
- Pricing-table changes do not retroactively recalculate historical charged estimates without preserving the original rate inputs.

---

## #86 — P2: SSE buffers the full reply and simulates streaming afterward

Issue: [#86](https://github.com/EvanProgramming/OpenKyrozen/issues/86)

### Observed behavior

In a timed live request:

- response headers arrived at 0.017 seconds;
- the first assistant content arrived at 28.222 seconds;
- every remaining chunk, cost event, and `[DONE]` arrived by 28.366 seconds.

The UI displayed `Thinking...` for the complete provider call and then received a rapid burst.

### Root cause and affected components

`server.api_chat_stream()` awaits the complete synchronous `_run_session_chat()` result in a worker thread, slices the finished string into 20-character pieces, and adds artificial 10 ms sleeps. It does not connect provider streaming to SSE.

### Required outcome

Design a real streaming orchestration path. It must support incremental provider output without bypassing tool execution, task tracking, session persistence, memory receipts, plugin hooks, webhook completion, error handling, or usage accounting.

### Acceptance tests

- A timing-controlled fake provider proves the first SSE content arrives before provider completion.
- Unicode split across transport reads is reconstructed exactly.
- `[DONE]` appears once and only after all content, usage, receipt, persistence, and completion state are correct.
- Provider errors before and after partial output produce coherent terminal events.
- Client disconnect cancels or safely drains work according to a documented policy.
- `chat.completed` webhook fires only for a successful completed stream.
- Real Chrome visibly receives incremental content during a deliberately delayed response.

---

## Cross-issue architectural requirements

### Structured execution receipts

Issues #74 and #80 expose the same design weakness: callers infer execution truth from model text or formatted tool output. A shared receipt should carry at least:

- canonical tool name and normalized arguments;
- authorization and approval decision;
- started/completed timestamps;
- exit code or typed failure category;
- success status;
- bounded redacted result;
- verified effect and acceptance evidence;
- stable operation identity for duplicate detection.

Presentation strings should be derived from this receipt, never used as the authoritative status source.

### Durable usage ledger

Issues #77, #78, #79, #84, and #85 require one coherent solution. Each provider attempt should record:

- provider and exact model;
- request/surface/session/run identity;
- input cache-hit and cache-miss tokens;
- output and reasoning tokens when supplied;
- authoritative versus estimated fields;
- request start/end and latency;
- completion, interruption, failure, and fallback state;
- rate identifiers and effective pricing inputs;
- precise calculated amount without per-request cent truncation.

Aggregation for CLI, UI, `/api/cost`, and learning metrics must read this ledger rather than maintain independent counters.

### Shared session initialization

Issues #81 and #83 require REST, SSE, MCP, browser reload, and restart recovery to use identical safe defaults for:

- authenticated server actor;
- speaker, audience, and channel;
- authorized speakers;
- profile;
- workspace and session scope;
- recovered message history;
- creation and update timestamps.

No client-supplied speaker may acquire another owner's private-memory authority.

### Real streaming orchestration

Issues #77 and #86 must be solved together. Do not call provider streaming only for simple text while silently falling back to buffered behavior for tool turns without documenting the distinction. Define the event model for content, tool planning/execution, task progress, usage, memory receipts, errors, completion, and cancellation.

## Full validation after remediation

### Clean installation

```bash
audit_home="$(mktemp -d)"
env HOME="$audit_home" sh -c 'curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/main/install.sh | sh'
env HOME="$audit_home" PATH="$audit_home/.local/bin:$PATH" kyrozen --version
env HOME="$audit_home" PATH="$audit_home/.local/bin:$PATH" kyrozen --help
env HOME="$audit_home" PATH="$audit_home/.local/bin:$PATH" kyrozen-web --help
```

Use a disposable path and securely remove it afterward. Never echo or embed a provider credential in a command.

### Repository gates

```bash
make check
make lint
make docs-check
make shell-check
make test
make benchmark
make wheel-smoke
make docker-smoke
git diff --check
```

The final test count must be at least the audited baseline of 146, plus the new regression tests.

### Real runtime acceptance

1. Configure `deepseek-v4-flash` through the encrypted hidden-input flow.
2. Run synchronous, real provider-streaming, tool-calling, retry/error, usage, and cost paths.
3. Give the CLI a state-changing task and verify it completes once, updates task status, and exits cleanly.
4. Run all 31 tools with success and meaningful failure fixtures.
5. Exercise Git operations in a disposable working repository and local bare remote.
6. Exercise all documented REST/MCP routes in fresh and reused sessions.
7. Make MCP `chat/send` the first request after a clean server start.
8. Start Chrome, verify incremental Unicode streaming, switch between two sessions, reload, restart the server, and restore both histories.
9. Verify visible and API cost totals before and after restart.
10. Verify a non-zero durable shell task remains failed across recovery.
11. Verify a live sub-agent stores real provider/model, token, and latency fields.
12. Re-run all 20 learning executors and inspect their durable started/completed/failed events.

### Documentation acceptance

Compare observed behavior with:

- `README.md`, `README.zh-CN.md`, `README.ja.md`, and `README.ko.md`;
- `docs/tool-inventory.md` and `docs/self-evolution.md`;
- `install.sh`, `install.ps1`, `Makefile`, `pyproject.toml`, and examples;
- environment variables, model names, pricing date/rules, routes, tools, capabilities, and entry points.

Regenerate the tool inventory and ensure generated files are reproducible. Do not retain claims such as “real-time streaming,” “session management,” “all extras,” or accurate cost tracking until the real acceptance flows support them.

## Completion definition

The remediation is complete only when:

- every issue #73–#86 has a verified root-cause fix;
- each issue has a regression test and live acceptance evidence where applicable;
- clean installation works from the exact public documentation;
- the full test/build/browser/container matrix passes, or an environmental blocker is explicitly documented;
- all README languages and generated inventories match runtime behavior;
- no credential or private audit artifact is committed;
- no issue is closed merely because its code compiled or a mocked unit test passed.

The original audit intentionally excluded Windows/Linux runtime acceptance and providers other than DeepSeek. Those surfaces still require separate platform/provider validation before making an unrestricted “works everywhere” claim.
