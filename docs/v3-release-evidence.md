# V3 release evidence

Release publication is blocked until signed records for R250–R266 all pass against the exact clean candidate commit. Each record is detached-GPG-signed by the pinned release-owner key, includes command and fixture files with verified SHA-256 digests, runner/OS/Python and dependency details, and references hashed result artifacts. Failed or inconclusive records must identify a failure class and can never produce a passing manifest.

R266 carries the supported-environment matrix: Linux, macOS, and Windows on Python 3.12 and 3.13. Each of its six platform records must show passing unit, integration, and acceptance checks plus at least 1,800 seconds of soak evidence. Each platform entry names nonempty `evidence_paths`, and every path must reference a hashed artifact in the enclosing record.

Store each record and its artifacts under one evidence directory, using these names:

```text
R250.json
R250.json.asc
...
R266.json
R266.json.asc
commands/R250.txt
fixtures/v3-fixtures.json
results/...
```

Records use schema version 1. Their required fields are `gate_id`, full `candidate_sha`, `command_set_path` and `command_set_sha256`, `fixture_set_path` and `fixture_set_sha256`, `environment` (`runner`, `os`, and `python`), `dependencies`, `result`, `failure_class`, and a nonempty `evidence` array of relative artifact paths and SHA-256 hashes. R266 also requires the six `platform_results` described above, each linked to its own hashed logs. Artifacts and records may not resolve outside the evidence directory.

After all records have been collected, validate and sign the aggregate manifest from a clean checkout. Keep both output files outside the checkout so writing the manifest cannot change the candidate SHA:

```sh
python -m benchmarks.v3_release_gates \
  --repo . \
  --evidence-dir /outside/checkout/v3-evidence \
  --manifest /outside/checkout/v3-release-manifest.json
```

The command verifies every record signature against the pinned GPG fingerprint, checks the exact checkout SHA, validates all artifacts and platform gates, then emits a signed manifest and detached signature. Any missing, stale, malformed, failed, unsigned, or inconclusive evidence returns failure and emits no manifest. It publishes the signature first and the manifest last, and refuses to overwrite either existing output; use a new manifest path for each attempt. The signed manifest records every gate record hash, command-set hash, fixture-set hash, environment, dependency inventory, and artifact hash for later publication or audit.
