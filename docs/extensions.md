# Skills, plugins, and learned artifacts

OpenKyrozen loads reusable guidance from bundled skills, user-installed skill packages, project instruction files, and Python plugins. Their roles and trust requirements differ; a file's name or manifest does not make its contents safe.

## Skills

Use `/skills` to inspect available skill versions and `/api/v2/skills` in the authenticated web interface to inspect installed records. Bundled packages live in `builtin_skills/` and include a manifest, instructions, and applicable license or notice. User or project skills are read as instruction content. The registry checks package structure and selection status; skill text can still be wrong or malicious.

Before installation or activation, review the source, publisher, requested behavior, license, and external commands. Pin or record the source when repeatable deployments matter. Skills inform how the agent approaches a request; they do not override user intent, mode restrictions, capability policy, approvals, or the tool executor. Roll back a skill if the active version is harmful or no longer appropriate.

## Project instructions and examples

`agent.yaml` can set a role, system text, examples, and an upper-bound capability profile. A project-local configuration overlays packaged defaults; an explicitly selected configuration file overlays that; recognized `KYROZEN_AGENT_*` and related environment values apply last. The loader rejects unknown fields and malformed shapes. The effective capabilities are still intersected with the active runtime policy. Never store credentials in `agent.yaml`.

See the sample [`agent.yaml`](../agent.yaml) and [configuration guide](configuration.md) for the accepted schema. Project-provided instructions and examples are untrusted input; inspect repository state and verify any requested operation on its actual target.

## Python plugins

The plugin lifecycle supports project Python extensions such as the sample `plugins/turn_logger.py`. Plugins execute Python in the agent process with its OS account's permissions. Read the implementation before loading a plugin; use an isolated test home and workspace when developing one. Hooks observe or extend defined runtime lifecycle points but cannot be assumed side-effect free. Record what data a hook can see and where it writes it, and test failure handling so a faulty extension does not mask task outcomes.

## Dynamic tools

Runtime-created dynamic tools are a distinct feature from skills. They remain disabled unless the operator enables them with `KYROZEN_ALLOW_DYNAMIC_TOOLS` and all normal tool capability and approval checks still apply. Review generated source and permissions before activation. A learning feature that observes successful tools does not silently switch this policy on.

## Learned policies and skills

Self-learning produces bounded candidate artifacts and records their evidence, validation, applicability, promotion, retirement, and rollback. A learned skill or policy is not trusted merely because a model wrote it or an earlier task used it successfully. Promotion gates and artifact import/export are documented in [self-learning](self-evolution.md). Review capsule provenance before importing one.

## Troubleshooting

If `/skills` does not show an installed package, check the selected skill root, manifest, activation state, and compatibility diagnostics. If plugin loading fails, inspect the sanitized startup diagnostic and import path; do not solve an import problem by adding a credentials file or private environment to a public report. Restore a prior skill through its documented rollback path; disable or remove untrusted extensions only after recording any state they own.
