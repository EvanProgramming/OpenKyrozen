# V3 release evidence

Full release publication is blocked until signed records for R250–R266 all pass against the exact clean candidate commit. Each record is detached-GPG-signed by the pinned release-owner key, includes command and fixture files with verified SHA-256 digests, runner/OS/Python and dependency details, and references hashed result artifacts. A failed gate, a missing record, or missing/failed safety evidence is never waivable.

A clearly labeled limited-evidence preview is available only for the fixed scope `openkyrozen-v3-preview-linux-macos-sandbox-required-v1`. Its only waivable inconclusive gates are R259 (native protocol recovery target), R262 (dynamic replanning quality), R263 (matched-model task success), and R264 (matched-task request/token efficiency). These are evaluation outcomes, not sandbox, file-patching, dynamic-tool-registration, or migration gates. R259 records must include `protocol_recovery_count`: a pass requires zero, and any observed positive count is a non-waivable failure even if mislabeled inconclusive. An interrupted run may record `null` or a partial zero count and remain inconclusive. A preview waiver means that the corresponding evaluation is incomplete; it never means that its target passed. Every other gate must pass. The evidence records must still say `inconclusive`; the manifest preserves that result and marks the corresponding operational status `waived`. The ordinary/full validator remains blocked by any inconclusive gate.

Each inconclusive evaluation gate needs its own detached-GPG-signed waiver in `waivers/R259.json` (and matching `.json.asc`), signed by the pinned release-owner key. The waiver must bind its gate ID, exact candidate SHA, the fixed release scope, approver fingerprint, rationale, limitations, hashes of its evidence references, and exactly one future expiry or review condition. Waiver evidence must include that gate's signed inconclusive record, R266's signed platform record, and all Linux/macOS Python 3.12/3.13 full unit logs. Every waiver independently requires those logs to show the patch-safety, sandbox-enforcement, dynamic-tool-registration, migration, and approval-history safety tests passing; R266 must bind those logs as platform artifacts. A failed gate or missing/failed safety test is never waivable. The release scope is preview-only for Linux and macOS with `KYROZEN_SANDBOX_REQUIRED=1`; Windows distribution, production use, general availability, and a claim that the full V3 evaluation passed are outside this scope.

R266 carries the supported-environment matrix: Linux, macOS, and Windows on Python 3.12 and 3.13. Linux and macOS records must show passing full unit and gateway integration suites. Windows records mark those POSIX-dependent suites `not_run` and must show passing supported-feature workflow acceptance, including Go TUI tests and build. The interactive TUI PTY scenario is explicitly recorded as `not_run_posix_pty_unavailable` on Windows because its harness requires POSIX `fcntl`/`pty`; it must pass on Linux and macOS. All six records require passing supported-feature acceptance and at least 1,800 seconds of soak evidence. Each platform entry names nonempty `evidence_paths`, and every path must reference a hashed artifact in the enclosing record.

The CI workflow runs the R266 matrix for pull requests or pushes that change its command set, fixture set, collector, or soak script. For an explicit run, dispatch `.github/workflows/ci.yml` with `v3_release_candidate_sha` set to the full candidate commit SHA. It checks out that exact SHA, runs full Python and gateway integration suites on Linux and macOS, and runs agent/sub-agent acceptance plus Go TUI tests and builds on all six platform/Python combinations. The POSIX interactive TUI PTY scenario runs on Linux and macOS only; Windows records its unavailability. Each platform also runs a 30-minute production soak. Windows records its Python unit and gateway integration checks as `not_run`; its supported-feature acceptance is required to pass. The workflow uploads per-platform logs even on failure for diagnosis and emits an R266 bundle only after every matrix job passes. The collector copies and hashes the command workflow, frozen fixture set, platform metadata, test logs, and soak results. The bundle is intentionally unsigned because the release-owner private key must stay off CI. Download it, place it in the evidence directory outside the candidate checkout, inspect the evidence, and sign `R266.json` with the pinned release-owner key before validating the complete release bundle:

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

For a limited-evidence preview, provide the fixed scope and place the four signed waivers under the evidence directory:

```sh
python -m benchmarks.v3_release_gates \
  --repo . \
  --evidence-dir /outside/checkout/v3-evidence \
  --manifest /outside/checkout/v3-preview-manifest.json \
  --limited-evidence-preview \
  --release-scope openkyrozen-v3-preview-linux-macos-sandbox-required-v1
```

The command verifies every record and waiver signature against the pinned GPG fingerprint, checks the exact clean checkout SHA, validates all artifact hashes and platform gates, then emits a signed manifest and detached signature. It verifies, parses, and hashes each signed record from one private snapshot so a file replacement during validation cannot change the bytes being accepted. Any missing, stale, malformed, failed, unsigned, or unsupported inconclusive evidence returns failure and emits no manifest. Preview manifests use `result: limited-evidence`, `release_classification: limited-evidence-preview`, and explicitly list `waived_gates`; full manifests continue to use `result: passed` only when every gate passes. Both manifest forms preserve per-gate `evidence_result`. The command publishes the signature first and the manifest last, and refuses to overwrite either existing output; use a new manifest path for each attempt. The signed manifest records every gate record hash, command-set hash, fixture-set hash, environment, dependency inventory, artifact hash, and waiver/signature hash for audit.

## Publishing the preview

The planned preview uses package version `3.0.0.dev1` and tag `v3.0.0.dev1`. Review and sign the final commit before collecting its evidence: changing the commit invalidates earlier records. The ordinary tag workflow excludes V3 tags and rejects manual V3 release requests, so it cannot create a full release for the preview tag.

After the exact candidate, signed gate records, scoped waivers, and signed manifest have all been reviewed, use the local publisher with an authenticated GitHub CLI and the release-owner signing key. Its output directory must be a new directory outside the checkout:

```sh
python -m scripts.publish_v3_preview \
  --repo . \
  --evidence-dir /outside/checkout/v3-evidence \
  --manifest /outside/checkout/v3-preview-manifest.json \
  --output-dir /outside/checkout/v3-preview-assets
```

The publisher verifies the commit signature, the signed manifest, every gate and waiver again, and the release workflow's V3 guard. It builds from the committed Git tree, signs an artifact hash record, and creates a signed tag bound to that commit. It creates a **draft prerelease**, downloads and hashes its assets, revalidates the evidence, and only then publishes it as a prerelease. Release notes identify every waived gate and its limitations. If a tag already exists, its signature and remote identity must match before retrying. If publication or its final verification is interrupted, inspect the GitHub release and tag before retrying; the publisher never overwrites an existing release.

Preview usage is limited to Linux and macOS with the filesystem sandbox required. Set `KYROZEN_SANDBOX_REQUIRED=1` when running `kyrozen` or `kyrozen-web`; unavailable required protection blocks execution. Preview `/update` verifies the version at the selected main-branch revision and refuses to stage an older or unverifiable version. It supports numeric release, `a`, `b`, `rc`, and `.dev` versions; an unsupported version format is rejected until the user installs an appropriate release explicitly. Windows remains in the acceptance matrix, but is outside preview distribution scope.
