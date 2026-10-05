# Engineering reports and evidence

This page indexes records of work performed at a stated revision and time. A report's test count, feature matrix, provider trial, and limitations describe that run. They do not automatically describe today's branch, a different provider/account, or a later dependency version.

## Historical validation records

| Record | Date and role | Evidence and limits |
|---|---|---|
| [Production audit](production-audit-2026-10-04.md) and [machine-readable receipt](production-audit-evidence-2026-10-04.json) | 4 October 2026. Earlier HOLD snapshot. | Superseded for its unresolved release gates by the following shipping audit. Preserve its commit identities and result details as originally recorded. |
| [Production shipping audit](production-shipping-audit-2026-10-04.md) and [shipping receipt](production-shipping-evidence-2026-10-04.json) | 4 October 2026. Follow-up covering the tested candidate and published v2.0.5. | Includes platform-specific validation and explicit unresolved live-provider/deployment limits. Results refer to the recorded revisions; the later v2.0.6 installer repair is documented in its release. |
| [Self-learning switch audit](self-learning-audit-2026-10-03.md) | 3 October 2026. | Controlled scenarios establish switch effects, not general task-quality superiority. The report identifies live-provider and quality comparisons that were not established. |
| [Modular-refactor validation](modular-refactor-validation.md) | 30 September 2026. | Reports the refactor's test counts and interface checks at that time. |
| [Decision Assist validation](decision-assist-validation.md) and [System One benchmark](system-one-benchmark.md) | Dated feature and benchmark evidence. | Interpret each trial using its stated backend, sample, holdout, and calibration limits. |
| [Fast-mode benchmark](fast-mode-benchmark.md) and [prompt/discovery comparison](prompt-discovery.md) | Dated, bounded measurements. | Small or incomplete samples do not establish general speed, quality, or token savings. |

## Machine-readable artifacts

`docs/production-*-evidence-2026-10-04.json` stores acceptance receipts and provenance. `docs/benchmarks/` contains sanitized comparison inputs. Treat these files as evidence for their named run, not as configuration files. Do not remove original checksums, failure records, revision hashes, or timestamps when adding explanatory context.

## Reproduction

Use the [development guide](development.md) for checks on the current checkout. When reproducing an old report, use its recorded revision, interpreter, dependencies, platform, browser/vector configuration, and command. Mark a result unverified when the exact environment or external service is unavailable; do not substitute the latest test count for evidence at an older commit.
