# Security, permissions, and deployment

OpenKyrozen runs local code with the permissions of the operating-system account that launches it. Its capability and approval layers restrict agent requests inside the application. They are policy controls, not a container, system-call filter, or operating-system sandbox.

## Interaction modes

| Mode | Intended use | Agent's execution authority |
|---|---|---|
| Ask | Explanation, inspection, and research | Read and network capabilities only |
| Plan | Investigate and prepare a reviewable proposal | Read and network capabilities only |
| Agent | Complete work after the user request or plan acceptance | Configured surface capabilities, approvals, and tool policy |
| Auto | Route a request to Ask or Plan | Inherits the selected read-only mode |

Accept a plan explicitly in the interface or with `/plan accept`. A model response, sub-agent suggestion, memory record, or downloaded instruction cannot grant permission. Execution receipts record the action and its outcome. Approval prompts alone do not contain a hostile program running as your user.

## Capabilities and approvals

The workspace's `agent.yaml`, environment overrides, permission settings, and active UI surface participate in the effective policy. Effective capabilities are intersected with the surface's policy; a workspace configuration cannot widen a read-only Ask or Plan turn. Review `capabilities:` before running untrusted tasks. The [runtime inventory](tool-inventory.md) shows each action's required capability. Web and MCP tools are filtered by their configured capability sets; dangerous MCP execution additionally requires its explicit opt-in.

Shell commands pass through command policy and tool approval checks. The browser uses isolated profiles, but its network access remains relevant to the environment. Repository and filesystem tools use the active workspace boundary; advanced Git actions can change local history or remotes and require particular care. Keep secrets out of files an agent is asked to read, because they are data visible to that process and potentially to the configured model provider.

## Tool output, memory, and extensions

Treat model output, shell output, web pages, repository files, saved memory, skill content, and sub-agent results as untrusted input. They are never approval grants or runtime instructions with higher authority. OpenKyrozen filters known prompt-injection patterns at input boundaries and preserves policy checks when an action is requested. These controls lower risk; they cannot prove arbitrary content harmless.

Downloaded skills and plugins execute or influence behavior according to their implementation. Inspect source and provenance, enable only what you need, and remove or roll back an extension you do not trust. Learned policies and skills have size, permission, verification, and rollback gates; they cannot add capabilities. Dynamic tools remain separately gated by `KYROZEN_ALLOW_DYNAMIC_TOOLS` and capability policy. See [extensions](extensions.md) and [self-learning](self-evolution.md).

## Server authentication

Loopback web requests may be made without a token. If `KYROZEN_SERVER_TOKEN` is set, API and MCP callers must send it as `Authorization: Bearer TOKEN` or `X-Kyrozen-Token: TOKEN`. The browser login route exchanges the configured token for a short-lived HttpOnly browser session; cookies are not API bearer tokens. Without a token, direct non-loopback requests are rejected.

```sh
export KYROZEN_SERVER_TOKEN="a-long-random-secret"
kyrozen-web --host 0.0.0.0 --port 8000
curl -H "Authorization: Bearer $KYROZEN_SERVER_TOKEN" \
  http://127.0.0.1:8000/api/health
```

Terminate TLS at a trusted reverse proxy when traffic leaves the machine. Restrict network ingress and egress at the firewall or proxy; application authentication does not encrypt HTTP traffic and outbound webhook validation does not replace network egress policy. Avoid command examples that write tokens to shell history. Rotate a token by changing the environment value and restarting the server.

## Data handling

Provider credentials are stored in environment values or the encrypted `~/.kyrozen_config.json` flow. SQLite holds local session, event, claim, task, usage, and learning records. Chroma is a derived index but may contain private text too. Back up, transfer, and delete both according to the sensitivity of the workspace. Do not attach databases, provider logs, config files, or user state to public issues. See [memory and storage](memory-and-storage.md).

## Reporting a vulnerability

Use [private vulnerability reporting](../SECURITY.md). Do not publish an exploit or confidential data in a regular issue.
