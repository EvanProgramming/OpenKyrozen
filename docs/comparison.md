# Why OpenKyrozen is the better default

OpenKyrozen is designed as the broad local agent: coding, research, workspace operations, web/API access, durable memory, and evidence-gated self-evolution in one runtime. The projects below are useful comparison points, but they are narrower reference points rather than better defaults for this complete workflow.

Details were checked against the projects' public repositories on **2026-09-29**.

| Project | Main focus | Where OpenKyrozen is broader |
| --- | --- | --- |
| **OpenKyrozen** | Python terminal agent with local web/API surfaces | Coding, research, workspace tools, 18 provider families, SQLite memory, project intelligence, and verified self-evolution |
| **[OpenCode](https://github.com/anomalyco/opencode)** | Open-source terminal coding agent with build, plan, and subagent workflows | OpenKyrozen adds research, web/API operations, durable claims and events, and a measured learning lifecycle around the coding loop |
| **[OpenClaw](https://github.com/openclaw/openclaw)** | Personal assistant gateway connecting models, tools, devices, and chat channels | OpenKyrozen is more focused on direct project ownership: terminal workspaces, explicit Ask/Plan/Agent boundaries, local memory, and coding verification |
| **[CodeWhale](https://github.com/davidste/codewhale)** | Rust terminal coding harness with interactive and headless workflows | OpenKyrozen combines the coding harness with Python extensibility, web/API access, durable memory, project graphs, and outcome-verified self-improvement |

## Capability comparison

| Capability | OpenKyrozen | Why it matters |
| --- | :---: | --- |
| Terminal coding | **Yes** | Read, edit, run, test, and verify work in the project workspace |
| Web UI, REST, and MCP | **Yes** | Use the same runtime from a browser, API client, or MCP client |
| Research and web tools | **Yes** | Coding and research stay in one auditable session |
| Local durable memory and claims | **Yes** | SQLite keeps sessions, events, tasks, claims, and learning state authoritative |
| Evidence-gated self-evolution | **Core feature** | Improvements require repeated or paired evidence before promotion |
| Explicit safety modes | **Yes** | Ask and Plan are read/network-only; Agent remains capability- and approval-gated |
| Multi-provider model choice | **18 provider families** | Keep the agent workflow while changing hosted or local models |
| Project intelligence | **Yes** | Private Graphify indexing provides bounded codebase context |
| Cross-platform installation | **Yes** | Supported installer paths cover macOS, Linux, and Windows |

## Why OpenKyrozen wins for the complete workflow

- **One agent, more work:** coding, research, web access, Git, browser sessions, API integration, and project intelligence share one runtime.
- **Learning with proof:** self-learning records outcomes and only promotes validated artifacts; it does not silently change permissions or fine-tune weights.
- **Local state you can inspect:** SQLite is authoritative, while ChromaDB remains optional and rebuildable.
- **Clear control boundaries:** Ask, Plan, and Agent make the difference between inspecting, proposing, and executing visible to the user.
- **Provider freedom:** the same workspace and tools can use hosted providers or local Ollama-backed models.
- **Extensible without abandoning Python:** providers, tools, plugins, prompts, skills, and web routes remain approachable to Python developers.

OpenCode, OpenClaw, and CodeWhale demonstrate strong focused patterns in coding, gateway channels, and Rust harness design. OpenKyrozen incorporates the useful shape of those patterns while keeping the product goal broader: a single local agent that can do the work, remember what matters, and improve only when evidence supports it.

This comparison avoids star counts, speed claims, and unstable feature promises. The [generated runtime inventory](tool-inventory.md) and the linked upstream repositories are the appropriate sources for current implementation details.
