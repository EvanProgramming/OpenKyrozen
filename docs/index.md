# OpenKyrozen documentation

This index covers the current product guides, developer references, and dated engineering records. Start with a task below; use the linked source files when an implementation detail must be checked.

## Getting started

| I want to… | Read |
|---|---|
| Install, update, or recover OpenKyrozen | [Installation](installation.md) |
| Choose a workspace and learn the interaction modes | [Usage](usage.md) · [Commands](commands.md) |
| Choose a provider, set model defaults, or configure environment values | [Providers](providers.md) · [Configuration](configuration.md) |
| Understand what the agent can do and how components fit together | [Architecture](architecture.md) · [Runtime inventory](tool-inventory.md) |

## Product guides

| Area | Guides |
|---|---|
| Safety and deployment | [Security and permissions](security.md) · [API, web, and MCP](api.md) · [Troubleshooting](troubleshooting.md) |
| Data and outcomes | [Memory and storage](memory-and-storage.md) · [Tasks and automation](tasks-and-automation.md) · [History and rollback](history-and-rollback.md) · [Usage and cost](usage-and-cost.md) |
| Improvement and extension | [Self-learning](self-evolution.md) · [Extensions](extensions.md) · [Sub-agents](subagents.md) · [Project intelligence](native-project-intelligence.md) |
| Decision and prompts | [Jev Decision validation](decision-assist-validation.md) · [System One benchmark](system-one-benchmark.md) · [Compact prompts and discovery](prompt-discovery.md) · [Fast mode benchmark](fast-mode-benchmark.md) |
| Project direction | [Runtime roadmap](agentic-runtime-roadmap.md) · [Project comparison](comparison.md) |

## Build, contribute, and review evidence

- [Development guide](development.md) and [contributor guide](../CONTRIBUTING.md) cover setup, layout, checks, and pull requests.
- [Reports index](reports.md) explains the dates, scope, and limits of recorded audits, validation runs, and benchmarks.
- [Modular refactor validation](modular-refactor-validation.md), [production audit](production-audit-2026-10-04.md), [shipping audit](production-shipping-audit-2026-10-04.md), and [self-learning audit](self-learning-audit-2026-10-03.md) preserve historical evidence.
- [V3 architecture ownership plan](superpowers/plans/2026-10-07-v3-issue-227.md) records the scoped implementation and verification for issue #227.
- Machine-readable audit receipts and benchmark inputs remain beside their corresponding reports under `docs/` and `docs/benchmarks/`.

The repository's [English overview](../README.md) links here. Technical guides are maintained in English; README translations provide localized entry points.
