# Security, permissions, and deployment

OpenKyrozen runs local code with the permissions of the operating-system account that launches it. Its capability and approval layers restrict agent requests inside the application. Managed shell commands also use an OS filesystem sandbox when a supported backend is available; this is a filesystem boundary, not a general-purpose container or complete system-call filter.

## Interaction modes

| Mode | Intended use | Agent's execution authority |
|---|---|---|
| Ask | Explanation, inspection, and research | Read and network capabilities only |
| Plan | Investigate and prepare a reviewable proposal | Read and network capabilities only |
| Agent | Complete work after the user request or plan acceptance | Configured surface capabilities, approvals, and tool policy |
| Auto | Route a request to Ask or Plan | Inherits the selected read-only mode |

Accept a plan explicitly in the interface or with `/plan accept`. A model response, sub-agent suggestion, memory record, or downloaded instruction cannot grant permission. Execution receipts preserve the call ID, canonical arguments, operation and scope identity, typed result, and the tool policy used for authorization. Optional task association is explicit and scope-checked; native action/argument similarity does not bind a receipt to a plan. Verification records distinguish observed effects from task acceptance, which still needs its own evidence. Audit projections are bounded and redact secret-like fields. Approval prompts alone do not contain a hostile program running as your user.

## Capabilities and approvals

The workspace's `agent.yaml`, environment overrides, permission settings, and active UI surface participate in the effective policy. Effective capabilities are intersected with the surface's policy; a workspace configuration cannot widen a read-only Ask or Plan turn. Review `capabilities:` before running untrusted tasks. The [runtime inventory](tool-inventory.md) shows each action's required capability. Web and MCP tools are filtered by their configured capability sets; dangerous MCP execution additionally requires its explicit opt-in.

Shell commands pass through command policy and tool approval checks. Typed shell calls confine their requested working directory to the active workspace, overlay environment values on OpenKyrozen's active environment, and return stdout and stderr separately. Before each command, OpenKyrozen probes that the selected backend can write in a temporary workspace and cannot write beside it. When available, Linux uses Bubblewrap to make the host filesystem read-only except for the workspace and a per-command temporary directory exposed as `TMPDIR`, and gives the command a private PID namespace. macOS uses the experimental `sandbox-exec` interface to restrict filesystem writes to the workspace and a per-command temporary directory exposed as `TMPDIR`; commands that hard-code other temporary paths may fail on either backend. Apple may change or remove this interface. Shell network access remains enabled on both backends, and neither backend imposes CPU, memory, or process-count limits. Descendant processes inherit the sandbox. If a backend is missing or its probe fails, the result explicitly reports that the command ran without filesystem protection; set `KYROZEN_SANDBOX_REQUIRED=1` to block shell execution instead. Windows currently has no filesystem sandbox backend and therefore reports unavailable, or blocks execution when required. A backend that passes the probe but then fails to start the command never retries unsandboxed. Background process handles are in-memory and scoped to their actor, workspace, and session; they stop when the application closes. The sandbox does not prevent commands from reading files their OS user can read, using network access, or exploiting weaknesses in permitted programs; approvals and OS sandboxing are complementary controls. Repository and filesystem tools use the active workspace boundary; advanced Git actions can change local history or remotes and require particular care. Keep secrets out of files an agent is asked to read, because they are data visible to that process and potentially to the configured model provider.

Bubblewrap is an optional Linux system package (for Debian or Ubuntu, install with `sudo apt install bubblewrap`). No Python dependency is added. The backend is exercised at runtime through the managed launcher and verified by platform acceptance tests; environments that disallow Bubblewrap's namespace setup will fail the command instead of receiving an unsandboxed retry.

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
