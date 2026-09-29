# Web, REST, and MCP API

Start the local server with:

```bash
kyrozen-web --host 127.0.0.1 --port 8000
```

The web UI and REST API share the same provider, workspace, capability, and approval boundaries as the terminal runtime. Streaming is available at /api/chat/stream; the health check is /api/health.

## Authentication

Localhost development may run without a token. For LAN or container access, set KYROZEN_SERVER_TOKEN and send it using the server's documented authentication flow. Never put a real token in a README, shell history, or issue.

## Contracts

The live FastAPI route table, runtime tool names, capability labels, and MCP schemas are generated from the running code. Read tool-inventory.md rather than maintaining a second hand-written route list.

Important surfaces include:

- chat and streaming: /api/chat, /api/chat/stream
- health and cost: /api/health, /api/cost, /api/cost/reset
- sessions: /api/auth/session
- memory: /api/memory
- v2 agents, events, learning, skills, and diagnostics under /api/v2/

## Example

```bash
curl http://127.0.0.1:8000/api/health
```

The generated inventory is checked by make docs-check. If a route or tool changes, regenerate it with:

```bash
venv/bin/python scripts/generate_tool_inventory.py --write
```
