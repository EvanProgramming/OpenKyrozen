# Code investigation fixture verification

**Goal:** Score source facts rather than incidental English phrases in all three investigation fixtures.

**Design:** Both arms receive the same explicit JSON answer schema, without the expected values. Compare typed facts and Python expression syntax, never execute model output. Require authorized, exact full-source read evidence and an unchanged workspace. Keep physical source proof separate from V2's error-word success heuristic and preserve its original workflow flags. Native receipts must also match their source path, hash, non-truncation flag and raw JSON. Permit JSON fences and the existing completed-plan annotation; reject other trailing text, duplicate keys and ambiguous answers. Add the missing POST route registration to the third fixture so the question is answerable from its source.

**Scope:** The three entries in `benchmarks/v3_fixtures.py`, their shared verifier in `benchmarks/v3_matched_live.py`, regression tests and this documentation. Product APIs, provider selection, permissions, statistics and release thresholds stay unchanged. A phrase-alias patch would retain language-dependent scoring; a model judge would add cost and nondeterminism.

- [x] Reproduce the original false negatives from retained six-row evidence; preserve their outcomes and accounting.
- [x] Add failing tests for structured source facts, wrong facts, ambiguous JSON, exact types, file edits and missing read evidence.
- [x] Implement the shared check, update its existing caller test, and supply the missing route binding.
- [x] Initial focused regressions: 143 associated tests passed (4 privileged-runner skips). Follow-up review of all nine real read receipts found a physical/workflow distinction missed in the initial early review: V2 incorrectly marks source containing `ValueError` unsuccessful. Paid admission stopped before any successor model calls.
- [ ] Replay the captured legacy receipt and native structured receipts, reject incomplete/denied/inconsistent evidence, then independently review the physical-read repair before a fresh freeze.
- [ ] Sign a new candidate, run all final checks against that SHA, then start fresh cohorts. Old rows cannot be rescored into release evidence.

All seven empirical requirements and all four paid gates remain pending. Model calls remain stopped during this repair; no publication is authorized.
