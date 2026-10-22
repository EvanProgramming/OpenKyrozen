# V3 release evidence

Release publication is blocked until signed records for R250–R266 all pass against the exact clean candidate commit. Each record is detached-GPG-signed by the pinned release-owner key, includes command and fixture files with verified SHA-256 digests, runner/OS/Python and dependency details, and references hashed result artifacts. Failed or inconclusive records must identify a failure class and can never produce a passing manifest.

R266 carries the supported-environment matrix: Linux, macOS, and Windows on Python 3.12 and 3.13. Linux and macOS records must show passing full unit and gateway integration suites. Windows records mark those POSIX-dependent suites `not_run` and must show passing supported-feature workflow and TUI acceptance instead. All six records require passing acceptance and at least 1,800 seconds of soak evidence. Each platform entry names nonempty `evidence_paths`, and every path must reference a hashed artifact in the enclosing record.

The CI workflow runs the R266 matrix for pull requests or pushes that change its command set, fixture set, collector, or soak script. For an explicit run, dispatch `.github/workflows/ci.yml` with `v3_release_candidate_sha` set to the full candidate commit SHA. It checks out that exact SHA, runs full Python and gateway integration suites on Linux and macOS, and runs the supported-feature agent, sub-agent, and TUI acceptance on all six platform/Python combinations. Each platform also runs a 30-minute production soak. Windows records its Python unit and gateway integration checks as `not_run`; its supported-feature acceptance is required to pass. The workflow uploads per-platform logs even on failure for diagnosis and emits an R266 bundle only after every matrix job passes. The collector copies and hashes the command workflow, frozen fixture set, platform metadata, test logs, and soak results. The bundle is intentionally unsigned because the release-owner private key must stay off CI. Download it, place it in the evidence directory outside the candidate checkout, inspect the evidence, and sign `R266.json` with the pinned release-owner key before validating the complete release bundle:

The soak runner gathers memory, thread, handle, and descendant-process evidence through Linux `/proc`, macOS `ps`/`lsof`, and Windows PowerShell. It uses only the Python standard library and platform tools; it does not add a Python dependency.

```sh
gpg --batch --local-user D3897E8FF704149D609ACF878C118C04263D2A4C \
  --armor --detach-sign --output R266.json.asc R266.json
```

The collector rejects stale candidate SHAs, missing artifacts, failed checks, short soaks, unexpected platform artifacts, and existing output directories.

The dispatch workflow is evidence collection, not a substitute for inspecting its result. A failed matrix run produces diagnostic artifacts but no passing R266 record; rerun only after classifying the failure as product or runner/infrastructure.

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
