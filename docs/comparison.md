# OpenKyrozen vs other open-source agents

This is a capability comparison, not a speed, quality, or benchmark claim. It compares the documented product shape of OpenKyrozen, OpenCode, OpenClaw, and Codewhale as checked on **2026-10-05**.

The feature is the vertical axis; each agent is a horizontal column. `🟡` means the capability is documented but narrower, optional, experimental, or not the project's main purpose. `❌` means it is not a documented core capability in the sources linked below.

| Capability | **OpenKyrozen** | [OpenCode](https://github.com/anomalyco/opencode) | [OpenClaw](https://github.com/openclaw/openclaw) | [Codewhale](https://github.com/Hmbown/CodeWhale) |
| --- | :---: | :---: | :---: | :---: |
| Terminal coding workflow | ✅ | ✅ | 🟡 | ✅ |
| Read files, edit code, run commands, check work | ✅ | ✅ | 🟡 | ✅ |
| Dedicated read-only planning mode | ✅ | ✅ | 🟡 | ✅ |
| Approval and permission controls | ✅ | ✅ | ✅ | ✅ |
| Local web or API surface | ✅ | 🟡 | ✅ | ✅ |
| Web research tools | ✅ | ✅ | 🟡 | 🟡 |
| Messaging channels | ❌ | ❌ | ✅ | ❌ |
| Mobile or device nodes | ❌ | ❌ | ✅ | ❌ |
| Hosted and local model choices | ✅ | ✅ | ✅ | ✅ |
| Multi-agent or subagent workflows | ✅ | ✅ | ✅ | ✅ |
| Durable local sessions or state | ✅ | ✅ | ✅ | ✅ |
| MCP or comparable tool extensions | ✅ | ✅ | ✅ | ✅ |
| First-class claims, events, and learning ledger | ✅ | ❌ | ❌ | ❌ |
| Evidence-gated self-evolution | ✅ | ❌ | ❌ | ❌ |

## What the matrix means

OpenKyrozen combines terminal work, project-aware research, web/API access, durable state, and measured self-improvement in one runtime. The last two rows are deliberately narrow: they refer to OpenKyrozen's documented claims/events learning state and promotion gates, not to ordinary session history, plugins, skills, or project configuration.

The other projects have clear strengths, but those strengths point to different centers of gravity:

- **OpenCode** is a focused coding agent with terminal, desktop, and IDE surfaces, built-in Build and Plan agents, subagents, permissions, and web tools.
- **OpenClaw** is a self-hosted Gateway for an assistant across messaging channels, control UI, companion apps, and device nodes. It can connect tools and coding agents, but the Gateway and channel model is its center.
- **Codewhale** is a Rust coding agent with a terminal TUI, `exec` mode, local browser client, explicit Plan/Work/Operate modes, model fleets, MCP, and resumable workflows.

These differences describe distinct product scopes. OpenKyrozen's differentiators in this table are its local claims/events ledger and evidence-gated learning workflow; the other projects document different interface, channel, and coding-agent priorities.

## Live prompt and tool-discovery pilot

A bounded [DeepSeek V4 Flash comparison with installed CodeWhale](prompt-discovery.md#live-pilot-2026-10-01) reached its 100-request cap before completing matched repeats. It does not support a quality or token-savings advantage; compact mode remains optional.

## Sources and boundaries

The comparison uses current public documentation rather than star counts or marketing claims:

- **OpenKyrozen:** [README](../README.md), [architecture](architecture.md), [API guide](api.md), [self-evolution guide](self-evolution.md), and the generated [runtime inventory](tool-inventory.md).
- **OpenCode:** [repository README](https://github.com/anomalyco/opencode) and [agent documentation](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/agents.mdx).
- **OpenClaw:** [repository README](https://github.com/openclaw/openclaw), [feature guide](https://github.com/openclaw/openclaw/blob/main/docs/concepts/features.md), and [Gateway documentation](https://github.com/openclaw/openclaw/blob/main/docs/gateway/index.md).
- **Codewhale:** [canonical repository README](https://github.com/Hmbown/CodeWhale), [modes and permissions](https://github.com/Hmbown/CodeWhale/blob/main/docs/MODES.md), and [official product overview](https://codewhale.net/en/product).

Features change as these projects release. Re-check the linked sources before making a deployment or security decision. Approval prompts are not automatically a sandbox; review each project's security model before granting access to untrusted work.
