# Usage guide

This is the shortest path from an installed OpenKyrozen command to useful work.

## Choose a workspace

```bash
kyrozen                         # persistent global workspace
kyrozen --project /path/to/repo # operate on a project
kyrozen --global                # explicit spelling of the global mode
```

The same choice is available in the web server:

```bash
kyrozen-web --project /path/to/repo --port 8000
```

## Interaction modes

| Mode | Use | Boundary |
| --- | --- | --- |
| ask | Explain, inspect, and research | Read and network operations |
| plan | Explore a task and prepare a reviewable plan | Read and network operations |
| agent | Execute accepted or explicitly requested work | Configured capabilities and approvals |
| auto | Route each request to Ask or Plan | Inherits the routed mode |

Set the preference with /mode auto, /mode ask, /mode plan, or /mode agent. Plan acceptance is explicit: use the UI action or /plan accept.

## Common commands

| Command | Purpose |
| --- | --- |
| /provider | Change the active provider |
| /model | Pin one main-agent model or restore automatic selection |
| /api_key | Change the active provider key |
| /mode ... | Change interaction mode |
| /agent auto\|coder\|researcher | Select the learning profile |
| /skills | Show built-in, local, and learned skills |
| /graph status\|refresh\|open | Inspect or refresh the private project graph |
| /learning status\|metrics\|evidence <id> | Inspect learning state |
| /memory why <claim-id> | Inspect claim provenance |
| /self-learning | Configure learning features |
| /decision-assist jev\|kev\|off | Enable or disable Jev/Kev decision checks |
| /system-one jev\|kev\|off | Configure typed routing decisions |
| /update | Update the installed runtime and bundled tools |
| /quit | Exit |

Type / at the start of a line to open the command palette. A slash inside a URL, path, or normal sentence remains ordinary text.

## Web mode

```bash
kyrozen-web
open http://localhost:8000
```

For LAN or container use, bind explicitly and set a token:

```bash
KYROZEN_SERVER_TOKEN=change-me kyrozen-web --host 0.0.0.0 --port 8000
```

See the [API guide](api.md) for authentication, streaming, and route details.
