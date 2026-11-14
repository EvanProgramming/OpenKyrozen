# Ubuntu CI mirror repair

> Execute the existing V3 finalization plan inline, with focused regressions and independent review before freezing a new signed candidate.

**Goal:** Finish the unchanged Linux acceptance despite repeated Ubuntu repository DNS failures.

**Design:** A small shared CI helper rewrites only the Ubuntu archive base URLs in the runner's three standard APT source files to `https://mirrors.kernel.org/ubuntu/`. Suites, components, Ubuntu signing keys, signature checks, package lists and test commands remain unchanged. This is CI preparation, not a product provider or execution service.

Repeated job retries preserved the failure. Waiting for primary DNS recovery has an unknown duration. A signature-verified mirror is the selected operational workaround: its base, updates and security indexes verified with the existing Ubuntu Archive signing key; updates/security were dated November 11. No extra package trust is introduced.

**Files:** `scripts/ci_ubuntu_mirror.py`, its focused tests, and the Linux dependency steps in `ci.yml` and `v3-benchmark-isolation.yml`.

- [ ] Test legacy and deb822 sources, mirror lists, unrelated repositories, URL boundaries, idempotence and unknown layouts.
- [ ] Implement the rewrite without new dependencies or model/runtime changes.
- [ ] Verify real APT metadata retrieval and Ubuntu signatures in a disposable configuration; retain both failed CI attempts.
- [ ] Independently review the helper, callers and evidence before signing.
- [ ] Freeze a new candidate and rerun complete acceptance; previous SHA outcomes remain historical.

Model requests remain stopped until the new candidate's complete offline admission passes. No release publication is authorized.
