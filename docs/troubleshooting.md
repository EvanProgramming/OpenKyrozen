# Troubleshooting

Start by recording `kyrozen --version`, the operating system, Python version if relevant, the launch command, and the exact sanitized error. Do not paste API keys, `~/.kyrozen_config.json`, session databases, browser profiles, private workspace contents, or unredacted audit logs into an issue. Send security vulnerabilities through the private route in [SECURITY.md](../SECURITY.md).

## Install, update, and start

| Symptom | Check | Recovery |
|---|---|---|
| `kyrozen` is not found | The installer may have added `~/.local/bin` without updating this shell's environment. | Open a new shell and check `PATH`; follow the installer message before reinstalling. |
| Installer cannot select Python | Use Python 3.12 or 3.13; confirm the network can reach the pinned GitHub release and `uv` bootstrap source. | Retry with the official installer after fixing the prerequisite. On Windows, use PowerShell and read the installer help. |
| Python works but the TUI is unavailable | Run `kyrozen --help`; inspect the update state and installed TUI version. | The Rich recovery UI can still handle setup and `/update`. Rerun the official installer if startup says update activation is incomplete. |
| `/update` did not finish | Read the structured update result and its recovery message. | Confirm disk space, network access, package-manager access, and that the app is closed when requested. Rerun the official installer after a partial activation. |
| Source install fails building extras | Confirm Python is in range and that platform/browser prerequisites exist. | Use `make install-core` for the smaller environment; see [development](development.md) for full checks. |

## Provider and model

Check that the selected provider name matches `/provider`, that its key is present in the launch environment, and that the model/deployment name is accessible to the account. Azure, Bedrock and Vertex also require their account, identity and regional settings. An alternate sub-agent provider needs its own credential and does not inherit the main key. If the service returns a model error, set an account-valid `KYROZEN_MODEL_SIMPLE` or `KYROZEN_MODEL_COMPLEX`; do not assume a registry default is enabled for every account. See [providers](providers.md).

## Workspace, permissions, and authentication

If the agent sees the wrong files, inspect `--project`, `KYROZEN_WORKSPACE_ROOT`, and `/project`; an existing session retains its original workspace. If an action is rejected, inspect the interaction mode, permission profile, required tool capability, and approval response. Ask/Plan cannot be made writable by model instructions.

For HTTP 401, provide a valid bearer token or `X-Kyrozen-Token`. For a 503 on a non-loopback connection, configure `KYROZEN_SERVER_TOKEN` and restart the server. Browser authentication uses a short-lived browser cookie after login; it does not authorize unrelated API clients. Do not put the token in a query string.

## Memory and learning

If recall changes while SQLite is healthy, check the actor/workspace/session scope and optional vector index. If the index is unavailable, retrieval can use the authoritative store; check disk access and rebuild only the derived index. Preserve the database before migration or manual recovery. See [memory and storage](memory-and-storage.md).

If learning shows `setup_required`, choose a Local or Remote learning provider in `/self-learning`. Local mode needs its approved local Ollama configuration; Remote reuses the configured provider. Learning is evidence-gated and a completed cycle may abstain without producing an artifact. Inspect `/learning evidence ID` and `/learning metrics` rather than counting worker cycles as improvements. For limits, see [self-learning](self-evolution.md).

## Browser, webhooks, and scheduled tasks

Browser actions require the optional Playwright integration and a usable browser installation. In a packaged/server setup, inspect browser diagnostics and launch-time environment instead of assuming a developer browser is shared.

Webhook delivery can fail without failing chat; inspect `WEBHOOK_FAILURE` audit entries and destination reachability. Avoid internal/private target URLs. Scheduled jobs run only while the web process is active; inspect durable status and receipts after restarting. Do not resubmit an uncertain external action before checking whether it already happened. See [tasks and automation](tasks-and-automation.md).

## Reporting a reproducible defect

Use the relevant issue form. Include the smallest reproduction and version details above, strip personal paths and names, and replace credential values with placeholders. For update or task defects include the status/result object and the check that failed; redact access tokens, request bodies containing private content, and response headers that can authenticate a session.
