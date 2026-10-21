# Skills, plugins, and learned artifacts

OpenKyrozen loads reusable guidance from bundled skills, user-installed skill packages, project instruction files, and Python plugins. Their roles and trust requirements differ; a file's name or manifest does not make its contents safe.

## Skills

Use `/skills` to inspect available skill versions and `/api/v2/skills` in the authenticated web interface to inspect installed records. Bundled packages live in `builtin_skills/` and include a manifest, instructions, and applicable license or notice. User or project skills are read as instruction content. The registry checks package structure and selection status; skill text can still be wrong or malicious.

Before installation or activation, review the source, publisher, requested behavior, license, and external commands. Pin or record the source when repeatable deployments matter. Skills inform how the agent approaches a request; they do not override user intent, mode restrictions, capability policy, approvals, or the tool executor. Roll back a skill if the active version is harmful or no longer appropriate.

The bundled `bug-fix` and `git-workflow` skills replace task-specific workflow
blocks in the global prompt. Matching skill content and version receipts are loaded
once during turn preparation, so activation or rollback changes affect later turns.
Prompt profile selection changes tool discovery detail only; native schemas and
the legacy text adapter remain separate invocation boundaries.

## Project instructions and examples

`agent.yaml` can set a role, system text, examples, and an upper-bound capability profile. A project-local configuration overlays packaged defaults; an explicitly selected configuration file overlays that; recognized `KYROZEN_AGENT_*` and related environment values apply last. The loader rejects unknown fields and malformed shapes. The effective capabilities are still intersected with the active runtime policy. Never store credentials in `agent.yaml`.

See the sample [`agent.yaml`](../agent.yaml) and [configuration guide](configuration.md) for the accepted schema. Project-provided instructions and examples are untrusted input; inspect repository state and verify any requested operation on its actual target.

## Python plugins

The plugin lifecycle supports project Python extensions such as the sample `plugins/turn_logger.py`. Plugins execute Python in the agent process with its OS account's permissions. Read the implementation before loading a plugin; use an isolated test home and workspace when developing one. Hooks observe or extend defined runtime lifecycle points but cannot be assumed side-effect free. Record what data a hook can see and where it writes it, and test failure handling so a faulty extension does not mask task outcomes.

## Tool registration and migration

Model-generated `DefineTool` blocks and `KYROZEN_ALLOW_DYNAMIC_TOOLS` are removed. Older configuration may still contain that environment variable, but OpenKyrozen ignores it; a model response containing `DefineTool` is rejected without compiling or registering its code. For compatibility, an old `dynamic` capability label in `agent.yaml` is accepted but grants nothing. Custom tools must be registered by application composition with a complete `ToolSpec` through `ToolRegistry.register_spec`, including an object schema, an existing capability, side-effect classification, and executor. Existing Python plugins provide lifecycle hooks; they do not register tools. The legacy `AVAILABLE_TOOLS` mapping can replace or remove registered tools, but it rejects new untyped callables. Python plugins still execute in-process with the OS account's permissions; review their source and provenance before loading them.

## Learned policies and skills

Self-learning produces bounded candidate artifacts and records their evidence, validation, applicability, promotion, retirement, and rollback. A learned skill or policy is not trusted merely because a model wrote it or an earlier task used it successfully. Promotion gates and artifact import/export are documented in [self-learning](self-evolution.md). Review capsule provenance before importing one.

## Troubleshooting

If `/skills` does not show an installed package, check the selected skill root, manifest, activation state, and compatibility diagnostics. If plugin loading fails, inspect the sanitized startup diagnostic and import path; do not solve an import problem by adding a credentials file or private environment to a public report. Restore a prior skill through its documented rollback path; disable or remove untrusted extensions only after recording any state they own.

## Tool catalog contracts

`ToolRegistry` owns canonical tool identity, schema, capability, risk, side-effect
class, parallel safety, visibility, aliases and version/source provenance.
`ToolSpec` binds that metadata to a workspace/runtime executor; `ToolManifest`
remains a compatibility projection. MCP `inputSchema` and provider-neutral
`parameters` are generated from the same defensive schema copy. Tool descriptions
in prompts, discovery and generated inventory come from registered specs.

`AVAILABLE_TOOLS` is a registry-backed compatibility mapping. Replacing an
executor retains its metadata, and removal removes its aliases and descriptors.
Adding an unregistered callable through this mapping raises `ValueError`; use
`register_spec` with complete metadata. Registration rejects duplicate names
and alias collisions; replacement requires `replace=True`. Schemas and alias
metadata are immutable, and snapshots preserve complete specs rather than only
callables. Registry metadata does not grant capabilities or bypass approvals.
Side effects are separate from capabilities: graph refresh can
update derived state under its existing read capability and remains nonparallel.
Browser, orchestration, memory, and process tools remain nonparallel. The pure
calculator and selected filesystem/Git reads are declared parallel-safe. The
foreground scheduler runs only consecutive catalog-approved, read-only calls
concurrently; every other call is a response-order barrier, and task receipts
are reconciled in that same order.

Typed invocation is canonical for native provider calls; legacy string
executors and MCP compatibility argument conversion remain at compatibility
boundaries. Tool inventory is generated with
`scripts/generate_tool_inventory.py --write` and checked by `make docs-check`.

Registration validates the supported object-schema vocabulary, including nested
properties/items/combinators and scalar constraints. Unsupported keywords are
rejected rather than forwarded unchecked; this registry is not a general JSON
Schema evaluation engine. Existing legacy argument conversion remains unchanged.


Built-in tools use stable result codes such as `invalid_arguments`, `not_found`, `permission_denied`, `approval_denied`, `dependency_unavailable`, `timeout`, `cancelled`, `command_failed`, `stale_file`, `conflict`, `execution_error`, and `contract_error`. Mutating tools keep failures non-retriable unless retry safety is explicitly established.
