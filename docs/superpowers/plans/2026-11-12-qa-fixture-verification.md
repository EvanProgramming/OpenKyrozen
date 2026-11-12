# Code investigation fixture verification

**Goal:** Score source facts rather than incidental English phrases in all three investigation fixtures.

**Design:** Both arms receive the same explicit JSON answer schema, without the expected values. Compare typed facts and Python expression syntax, never execute model output. Require a successful authorized read of the named source and an unchanged workspace. Permit JSON fences and the existing completed-plan annotation; reject other trailing text, duplicate keys and ambiguous answers. Add the missing POST route registration to the third fixture so the question is answerable from its source.

**Scope:** The three entries in `benchmarks/v3_fixtures.py`, their shared verifier in `benchmarks/v3_matched_live.py`, regression tests and this documentation. Product APIs, provider selection, permissions, statistics and release thresholds stay unchanged. A phrase-alias patch would retain language-dependent scoring; a model judge would add cost and nondeterminism.

- [x] Reproduce the original false negatives from retained six-row evidence; preserve their outcomes and accounting.
- [x] Add failing tests for structured source facts, wrong facts, ambiguous JSON, exact types, file edits and missing read evidence.
- [x] Implement the shared check, update its existing caller test, and supply the missing route binding.
- [x] Run focused regressions and independent review before freezing: 143 associated tests passed (4 privileged-runner skips), with no substantive finding in the early independent review.
- [ ] Sign a new candidate, run all final checks against that SHA, then start fresh cohorts. Old rows cannot be rescored into release evidence.

All seven empirical requirements and all four paid gates remain pending. Model calls remain stopped during this repair; no publication is authorized.
