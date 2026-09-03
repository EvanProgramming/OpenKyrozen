# Self-evolution and memory operations

This is the operator guide for the merged self-evolution and multi-party memory
features. It records behavior that is callable today, not a promise about
future learning. The verification below was run on `main` at `51d0bce` on
2026-09-03.

## Verified surface

| Surface | Current behavior | Verification |
| --- | --- | --- |
| Profile routing | `auto` routes normal requests to `coder` or `researcher`; an explicit profile wins. | `tests/test_evolution.py` |
| Evidence ledger | Runs, tool/error receipts, acceptance evidence, latency, tokens, and artifact hashes are durable SQLite events. | `tests/test_evolution.py`, `tests/test_evolution_robustness.py` |
| Canary lifecycle | A learned artifact stays a canary until two distinct verified successes and a non-regressing paired replay; correction or repeated verified failures rolls it back. | `tests/test_evolution.py` |
| Deterministic selection | Matching is profile- and trigger-scoped, with at most three artifacts and 8,000 body characters, including at most one canary. | `tests/test_causal_selection.py` |
| Retirement | A paired omission trial can retire an artifact only when completion does not regress; restore keeps the pre-image recoverable. | `tests/test_causal_selection.py` |
| Robustness | Environment/model mismatch, verifier reliability, constitution rules, and inactive capsule imports are enforced. | `tests/test_evolution_robustness.py` |
| Typed memory | Owner claims, inferred candidates, scope precedence, dependencies, and `/memory why|forget` are available. | `tests/test_memory_claims.py` |
| Multi-party memory | Attributed beliefs, private facts, and group agreements are filtered by speaker, audience, channel, and visibility; receipts identify speakers whose claims were used. | `tests/test_multi_party_memory.py`, `tests/test_server.py` |
| Harness-neutral benchmark | Clean/evolved runs and candidate, predecessor, omission, and no-memory ablations export JSON with a claim gate. | `learning_benchmark.py`, `benchmarks/multi_party_memory.jsonl` |

The implementation is local-only. It does not fine-tune weights, synchronize a
cloud memory, edit harness source, grant capabilities, or create dynamic tools.
The base `tools.py` registry exposes 29 actions; the terminal runtime adds
`search_memory` and `check_stored_data`, for 31 runtime actions.

## Terminal workflow

Start the agent with `make run` or `python main.py`. These commands are handled
by the running terminal session:

| Command | Use |
| --- | --- |
| `/agent auto` | Let task terms choose `coder` or `researcher`. |
| `/agent coder` | Force repository, terminal, and implementation learning. |
| `/agent researcher` | Force research, analysis, and source-bound writing learning. |
| `/learning status [coder\|researcher]` | List proposal stage, profile, uses, failures, and predecessor. |
| `/learning metrics [coder\|researcher]` | Show verified completion, corrections, repeated errors, tool calls, tokens, latency, and near misses. |
| `/learning evidence <proposal-id>` | Show the proof card, applicability, replay, outcomes, and metrics. |
| `/learning explain <proposal-id>` | Show the complete proposal record. |
| `/learning replay <proposal-id>` | Explain the API replay workflow; it never runs a command itself. |
| `/learning rollback <proposal-id>` | Roll back a learned proposal or its skill version. |
| `/memory why <claim-id>` | Inspect claim type, scope, authority, dependencies, and status. |
| `/memory forget <claim-id>` | Forget a claim and deactivate solely dependent learned artifacts/regressions. |

`/learning replay` is intentionally informational because replay results must
come from a separately sandboxed, frozen run. Submit those results through the
API below.

## How automatic evolution behaves

1. Each eligible run is recorded under one profile. Completed multi-step runs,
   explicit corrections, and verified failures may enter review. Provider
   failures, secret-bearing runs, routine one-step work, and project indexing
   are excluded from behavioral evolution.
2. The background loop reviews after at least 60 seconds without user
   interaction. A reviewer can create at most one bounded `policy` or `skill`
   canary for the run, or abstain.
3. Static validation requires the manifest, Markdown sections, size limits,
   secret redaction, workspace containment, and declared permissions. Learned
   artifacts cannot add capabilities or dynamic tools.
4. A matching canary is used at most once per run. Promotion requires two
   distinct verified successes, no correction or failed use, and a paired
   candidate-versus-predecessor replay that does not regress.
5. A verified artifact-linked correction, or two verified failures among its
   last five uses, rolls the artifact back to its predecessor. Every activation,
   promotion, retirement, and rollback is recorded as an auditable event.

For coding, a successful tool call is not enough: an acceptance command/test or
explicit user confirmation is required. For research, requested sources,
structure, or artifact creation must be observable, or the user must confirm
success. Otherwise the run remains unverified.

## Web API

Install the web extras and start the server:

```bash
pip install -e '.[web]'
KYROZEN_SERVER_TOKEN=change-me python server.py --host 127.0.0.1 --port 8000
```

Loopback requests may omit the token. Any non-loopback deployment must send
`Authorization: Bearer <token>` (or `X-Kyrozen-Token`) and should bind to an
explicit interface.

### Chat and profiles

`profile` is optional; `auto` is the default. Chat context can also carry a
speaker, audience, and channel. The response includes `memory_receipt` when
stored claims affected the turn.

```bash
curl -sS http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"message":"Write a sourced migration note", "profile":"researcher", "session_id":"demo", "speaker":"authenticated", "audience":"team", "channel":"chat"}'
```

The streaming endpoint `/api/chat/stream` accepts the same body and emits the
receipt as an SSE record when one is available.

### Inspect learning evidence

```bash
curl -sS 'http://127.0.0.1:8000/api/v2/learning?profile=coder&status=canary' \
  -H 'Authorization: Bearer <token>'
curl -sS 'http://127.0.0.1:8000/api/v2/learning/metrics?profile=coder' \
  -H 'Authorization: Bearer <token>'
curl -sS 'http://127.0.0.1:8000/api/v2/learning/<proposal-id>/evidence' \
  -H 'Authorization: Bearer <token>'
```

Submit a paired shadow replay only after both rows have been executed in a
side-effect-free sandbox. Candidate and predecessor arrays must be non-empty,
the same length, and contain identical unique `case_id` values:

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v2/learning/<proposal-id>/replay' \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer <token>' \
  -d '{"candidate":[{"case_id":"case-1","verified_success":true,"provider_model":"provider:model-a"}],"predecessor":[{"case_id":"case-1","verified_success":true,"provider_model":"provider:model-a"}]}'
```

Use `/omission` with matching `with_item` and `without_item` arrays to record a
candidate omission trial. `/retire` requires a non-regressing trial;
`/restore` returns a retired artifact to canary; `/rollback` immediately
restores its predecessor. `/capsule` exports redacted evidence and
`POST /api/v2/learning/capsules` imports it as an inactive candidate—import
never activates trust.

### Typed and multi-party claims

Create an attributed belief or group agreement with the claim endpoint:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v2/memory/claims \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer <token>' \
  -d '{"key":"release date","value":"Monday","claim_type":"attributed_belief","speaker":"alice","visibility":"group","audiences":["ops"],"channel":"release","authority":"owner"}'
```

The supported claim types are `general`, `attributed_belief`, `private_fact`,
and `group_agreement`. Filter retrieval before it reaches the model:

```bash
curl -sS 'http://127.0.0.1:8000/api/v2/memory?q=release%20date&speaker=alice&audience=ops&channel=release' \
  -H 'Authorization: Bearer <token>'
curl -sS 'http://127.0.0.1:8000/api/v2/memory/claims?speaker=alice&audience=ops&channel=release' \
  -H 'Authorization: Bearer <token>'
```

An attributed belief is always rendered with its speaker and does not resolve
to an unattributed value when speakers disagree. A private fact requires
`authority=owner` and is never exposed through unscoped recall. Private API
claims must name the authenticated request actor: `loopback` for an
unauthenticated loopback server, or `authenticated` when
`KYROZEN_SERVER_TOKEN` is enabled. A client-supplied label such as `alice` is
an attribution, not an authorization grant. Deployments that need distinct
human identities or group membership must enforce that mapping at their
authentication or proxy layer.

`GET /api/v2/events?event_type=memory.recalled` shows the receipt payload used
for a turn. Learning events such as `learning.outcome`,
`learning.artifact_promoted`, and `learning.artifact_rolled_back` provide the
corresponding audit trail.

## Benchmark replay

The benchmark runner is harness-neutral and reads one JSON object per case from
stdin. Each wrapper must emit these fields:

```json
{"verified_success":true,"corrections":0,"repeated_errors":0,"tool_calls":4,"tokens":1200,"latency":2.4}
```

Run the frozen multi-party cases with identical clean/evolved settings:

```bash
python main.py learning benchmark \
  --cases benchmarks/multi_party_memory.jsonl \
  --clean-runner './clean-wrapper' \
  --evolved-runner './evolved-wrapper' \
  --ablation 'candidate=./candidate-wrapper' \
  --ablation 'predecessor=./predecessor-wrapper' \
  --ablation 'omission=./omission-wrapper' \
  --ablation 'no-memory=./no-memory-wrapper' \
  --output benchmark.json
```

The JSON output preserves case order, per-case results, Wilson completion
intervals, secondary metrics, and ablations. The claim gate is true only when
completion is non-regressing and a paired secondary improvement is credible;
OpenKyrozen does not emit a competitor-superiority claim from a tool success or
an unpaired run.

## Verification and troubleshooting

Run the same local gates used for the merged implementation:

```bash
make check
tmpdir=$(mktemp -d /tmp/openkyrozen-check.XXXXXX)
KYROZEN_DB_PATH="$tmpdir/state.sqlite3" make test
git diff --check
```

The last verification on `main` passed 58 tests, the API health/scoping smoke,
the CLI command-loop smoke, and the five-case multi-party benchmark with four
ablations (`make check` reports the 29-entry base registry). A new artifact is
not immediate: wait for an eligible run, at least
60 seconds of idle time, reviewer evidence, and then the two-success plus
replay gate. A model or environment mismatch intentionally downgrades an
artifact to canary. If ChromaDB is unavailable, SQLite remains the durable
source and keyword recall continues to work.
