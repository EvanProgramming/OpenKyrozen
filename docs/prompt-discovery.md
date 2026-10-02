# Compact prompts and tool discovery

The prompt profile is optional. The default remains `classic`.

```bash
KYROZEN_PROMPT_PROFILE=compact kyrozen --project .
# From this source checkout:
KYROZEN_PROMPT_PROFILE=compact make run
# Restore the classic prompt:
KYROZEN_PROMPT_PROFILE=classic kyrozen --project .
```

The same environment variable applies to TUI and web processes. Invalid values
raise an explicit configuration error when the runtime builds the prompt.

Compact mode keeps full descriptions for `list_dir`, `find_files`, `read_file`,
`write_file`, `run_cmd`, and `discover_tools`. Other permitted tools appear by
exact name, grouped by capability. The agent can request their descriptions:

```text
Action:
{"action":"discover_tools","args":"git_log,git_show"}
```

An empty argument returns the permitted catalog. Named requests return complete
existing tool descriptions, including their argument instructions. Descriptions
remain available for the rest of the current turn, and reset on the next turn.
No descriptions or permissions transfer between sessions. Unknown or unavailable
names produce an error without revealing restricted tool descriptions.

Discovery intersects the configured capability bound, active surface token, and
interaction mode. Ask and Plan remain read-only. Tool execution retains its
existing capability, approval, workspace, and receipt checks. Dynamic tools remain
visible only with dynamic permission. MCP lists the new read-capability tool using
the existing generic `{ "args": "..." }` input contract.

Compact mode removes repeated workflow text and duplicate continuation guidance.
It preserves role configuration, untrusted-input handling, mode controls, the
Action protocol, durable task completion, and verification requirements. It does
not use another model or grant additional permissions.

## Live pilot: 2026-10-01

**INCONCLUSIVE. No superiority or token-savings claim is supported.**

The pilot compared baseline OpenKyrozen at `3536777`, compact OpenKyrozen, and
installed CodeWhale `0.9.12 (dcd4c200f72f)`. All **100 upstream requests** used
`deepseek-v4-flash`, the same credential and endpoint, thinking disabled,
temperature 0, top-p 1, and a 4,096-token output limit. All returned provider usage.
Total consumption was **222,375 input-plus-output tokens**.

The request cap was reached before the requested two repeats completed. Baseline
and compact each ran four fixtures once; CodeWhale ran three once. Raw arm totals
cover different workloads and cannot establish savings.

| Arm | Independent fixture checks passed | Complete runs passed | Requests | Total tokens |
|---|---:|---:|---:|---:|
| Baseline OpenKyrozen | 1/4 | 1/4 | 62 | 124,058 |
| Compact OpenKyrozen | 3/4 | 1/4 | 27 | 49,336 |
| Installed CodeWhale | 3/3 | 3/3 | 11 | 48,981 |

Compact produced code that passed the independent regression-fix and implementation
tests, but left pending durable tasks in both runs. Those runs fail overall
completion acceptance. Both OpenKyrozen arms failed the Git-history answer check.
CodeWhale's history fixture and all second repeats were not reached. These are
observations on this small workload, not general quality conclusions.

The initial CodeWhale adapter missed its `content` answer event. Its investigation
answer was inspected during the run and regraded from that exact event without
new requests. OpenKyrozen's initial tool counter was also unavailable because CLI
chat replaced the event callback. Unavailable metrics are marked null in the
[sanitized data](benchmarks/prompt-comparison-2026-10-01.json). Both adapters now have
regression coverage; their corrections have not been rerun live because the cap
is exhausted.

## Reproduce

Use Python 3.12/3.13 and installed CodeWhale. Preserve a baseline checkout:

```bash
mkdir -p /tmp/kyrozen-baseline
# Export the exact pre-feature commit, without local runtime data:
git archive 353677747bef090db4be3245560e2aac5bb04ff3 | tar -x -C /tmp/kyrozen-baseline
venv/bin/python benchmarks/prompt_comparison.py \
  --baseline /tmp/kyrozen-baseline \
  --output /tmp/prompt-comparison.json
```

Each invocation is a paid live run. The default limits are 100 requests and
1 million tokens; do not run again under an already exhausted budget. The runner
reads `DEEPSEEK_API_KEY` or the existing encrypted OpenKyrozen DeepSeek config.
Credentials never enter subprocess arguments or exported data. Missing credentials
or API incompatibility blocks the comparison.

The runner uses fresh temporary homes, deterministic Git fixtures, and real CLI
execution. It disables user plugins, background learning, System One, provider
fallback and model routing. It runs each product's prompt, planning, tools,
recovery, receipts and completion logic. CodeWhale uses `exec --auto
--output-format stream-json`; OpenKyrozen uses its production Rich CLI chat path.

A localhost relay preserves each client's messages and tool protocol, normalizes
only model/API settings, records usage, and reserves an upper input bound plus
maximum output before forwarding each request. Failed requests retain their
reservation when usage is unknown. Retries and summaries count toward the caps.
The relay converts a non-streaming upstream response into SSE when requested;
latency is therefore descriptive, not a production streaming-speed benchmark.

Original fixture tests run outside the agent and unchanged-file checks prevent
passing by deleting or weakening tests. Complete acceptance also requires all
OpenKyrozen durable tasks to succeed. The claim gate requires complete matched
pairs, identical passing outcomes, known usage, verified settings, and at least
10% lower aggregate total tokens. Reasoning tokens are not added again to
completion tokens. Incomplete, failing, or unknown-usage evidence yields no claim.
