# Self-learning audit — 2026-10-03

This audit traces the 20 switches through their trigger, durable product, and later use. **PASS in the matrix means an isolated repeatable scenario exercised that chain, often with a controlled model/tool response.** It does not mean the model improves arbitrary tasks. Live provider evidence and remaining coverage limits are separate below.

## Baseline and repairs

Installed `kyrozen --version` reported **2.0.4**. A read-only SQLite query of the user's existing state (no content read or changed) found 178,964 `learning.feature_completed` events, 69 proposals (6 active, 63 candidate), 5 active memories, 382 `memory.recalled` events, and no `learning.product_created` or `learning.product_used` receipts. Those aggregate counts show why a completed-cycle counter was insufficient evidence of learning.

The repaired checkout records products and actual prompt/selection use, distinguishes `none`, `candidate`, `available`, and `used` in CLI/TUI/web status, persists tool performance for detached analysis, and keeps context digests across restart. Conversation extraction now uses a durable cursor and retries a failed model call. Skill invention needs distinct observations before its workflow is active. Project indexing and technology research no longer report queued work as a completed product. Memory importance scores now participate before the final recall limit. Duplicate writes cannot widen private memory visibility. Verified corrections alone arm regression preflights; dynamic-tool guidance remains subject to current capability checks.

## Per-switch evidence

| Switch | Trigger | Durable product | Later effect | Evidence |
| --- | --- | --- | --- | --- |
| `auto_learn_conversations` | PASS | PASS | PASS | Two observed chats promote a fact; retry after model failure; restart and real DeepSeek/Qwen worker checks. |
| `load_project_files_into_memory` | PASS | PASS | PASS | Async Graphify callback records only a ready index; matching code query injects graph context. |
| `age_out_old_coded_entries` | PASS | PASS | PASS | Deleted `.py` snapshot removed from the file index; later lookup cannot return it. |
| `auto_debug_tool` | PASS | PASS | PASS | Repeated failure observations produce a scoped debug note recalled on a matching tool task. |
| `consolidate_memories` | PASS | PASS | PASS | Controlled consolidation produces a fact recalled later; source observations remain for correction. |
| `review_tools` | PASS | PASS | PASS | Durable tool statistics produce a review note recalled for that tool. |
| `targeted_inquiry` | PASS | PASS | PASS | Idle scan documents an undocumented function; matching function query recalls it. |
| `idle_reflection` | PASS | PASS | PASS | Eligible idle history produces a reflection used on a matching task. |
| `strategy_distillation` | PASS | PASS | PASS | High-cost turn history produces a strategy recalled later. |
| `auto_patch_technology` | PASS | PASS | PASS | Controlled successful search produces library guidance; queued fetch alone is not success. |
| `invent_skills` | PASS | PASS | PASS | Distinct conversation windows promote a candidate workflow; later skill composition sees it. |
| `context_compression` | PASS | PASS | PASS | Session digest persists in SQLite and is restored into a later prompt after restart; other sessions cannot read it. |
| `outcome_verified_evolution` | PASS | PASS | PASS | Existing canary, replay, promotion, retirement, restore, and scoped-use tests plus product/use receipts. |
| `dynamic_tool_definition` | PASS | PASS | PASS | Repeated successful calls promote a tool-use note; removal or denial hides it, and no tool is granted. |
| `detect_user_preferences` | PASS | PASS | PASS | Explicit preference promotes, survives restart, and changes prompt preference context. |
| `autonomous_inspection` | PASS | PASS | PASS | Idle project finding produces a note recalled for the matching code issue. |
| `memory_importance_scoring` | PASS | PASS | PASS | Persisted scores change which equally matching memory reaches a one-item prompt. |
| `knowledge_graph_extraction` | PASS | PASS | PASS | Extracted edge is stored and recalled for a matching entity query. |
| `skill_composition` | PASS | PASS | PASS | Two distinct runs promote a composed strategy; matching task recalls it. |
| `learning_rollback` | PASS | PASS | PASS | Verified correction creates a regression preflight used on the same task signature; unverified correction does not. |

The controlled scenarios are in `tests/test_learning_dispatcher.py`, with related evidence gates in `tests/test_evolution.py`, `tests/test_multi_party_memory.py`, `tests/test_learning_worker.py`, `tests/test_server.py`, and `tests/test_tui_protocol.py`. Enable/disable, persistent flag recovery, candidate versus active, private scope rejection, tool permission denial, and unmatched-task rejection are covered there. Multi-party web sessions intentionally do not feed global conversation extraction; a single-user web chat does.

## Real-provider and user-flow results

| Flow | Clean/no memory | Learned | Tokens (input/output) | Latency | Tools | Result |
| --- | --- | --- | --- | --- | --- | --- |
| Installed 2.0.4, Qwen2.5:7b | `UNKNOWN` | `orion.toml` | 2050/2; 2050/5 | 19.67s; 12.51s | 0; 0 | PASS: learned fact affected answer |
| Repaired checkout, Qwen2.5:7b | `UNKNOWN` | `orion.toml` | 2050/2; 2050/5 | 13.64s; 13.52s | 0; 0 | PASS: same effect; no measured quality gain over 2.0.4 |
| Repaired checkout, detached Qwen worker | N/A | Candidate then active fact after two idle cycles | Not captured per call | Two 30s cycles plus inference | 0 | PASS: separate process, durable product |
| Repaired checkout, DeepSeek Flash | N/A | Two calls promoted an Orion configuration fact and later recall included it | 126/15 each | 0.78s; 0.55s | 0 | PASS: remote product and use |

DeepSeek used **2 requests, 252 input tokens, 30 output tokens**. A temporary wrapper enforced 50 requests maximum, at most 20,000 input bytes (conservatively below the token cap for these prompts), `max_tokens=512` on each request, and a cumulative ¥2.50 stop. Usage readback gives a conservative **¥0.000744 peak-price upper bound**, well below ¥3. The [official DeepSeek CNY table](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) was rechecked before the calls: Flash peak uncached input ¥2/million tokens and output ¥8/million. No credentials or private user content entered fixtures or this report.

## Validation and limits

- `make test`: PASS, 409 tests in 89.076s.
- `make check`, `make lint`, `make docs-check`: PASS.
- Go tests in `tui`: PASS. Rebuilt TUI acceptance: PASS, including onboarding learning selection, chat, approvals, and resize.
- Wheel/sdist smoke outside checkout: PASS on the final rebuilt package; commands, resources, and runtime contracts ran from the isolated installation.
- Web `/api/v2/learning/features`, CLI switch persistence, and TUI status payload: PASS in isolated tests.
- **BLOCKED:** A live task-quality comparison for each of the other 19 switches was not established. Their product/use chains passed controlled scenarios, but those do not prove better responses on general tasks. The four-call Qwen pair showed equal quality for installed and repaired versions. No superiority claim is supported.
- **BLOCKED:** Installed 2.0.4 TUI and web interaction were not rerun as a separate baseline; installed CLI package identity and paired model responses were checked. The repaired TUI, web API, package, and worker were exercised.

All test state used temporary databases, skill roots, workspaces, and worker files. Existing user learning state was inspected with SQLite `mode=ro` using aggregate queries only.
