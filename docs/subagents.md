# Sub-agents

OpenKyrozen can delegate independent investigations or specialized work without
manual agent creation. The main agent receives delegation guidance at the start
and during action rounds. It stays direct for simple requests, coordinates
further exploration, and can reuse completed agents. Workers suggest followups
instead of spawning children. Concurrency defaults to four; additional work
queues without a fixed total-agent cap. Duplicate exploration without a distinct
objective or new context is rejected.

## Using sub-agents

Start OpenKyrozen in Agent mode and describe the work normally. For example:
“Review this service before launch. Find correctness and security bugs, explain
affected inputs, and give concrete fixes with file references. Please do not edit
anything.” You do not need to request agents or cross-examination. The main
agent decides whether distinct investigations justify delegation; a small
question such as “What does this function return?” can stay direct.

Open `/agents` to follow assignments and inspect their evidence and reviews.
Worker completion is followed by automatic independent review; disagreements
can trigger corrections. Check unresolved findings in the final answer, even
when some agents succeeded. Cancellation is available in the inspector.
To choose different providers for workers and reviewers, use the role settings
in [configuration.md](configuration.md) and configure each provider's credentials.

## Automatic coordination

After initial discovery in Agent mode, the main model explicitly chooses direct
work or complete specialist assignments in structured JSON. This decision uses
the original request, observed targets, active permissions and existing agents;
it is scoped in the event store. Closely related helpers share an investigation.
Simple requests stay direct, and accepted plans retain their existing task
contract. Invalid decisions get two repair attempts before ordinary continuation.

Each worker has a separate provider instance, session, messages, execution
context, compaction state, capability token and usage ledger. Agents share the
workspace and scoped SQLite services. Structured writes require ownership of
exact assigned files: disjoint writes can overlap, overlapping ownership waits,
and opaque commands/repository mutations serialize against active workspace
reads and writes. The same coordination guards main-agent actions. Capability
labels and ownership checks are application policy, not an OS sandbox.

## Assignment and result contracts

Actions retain the existing string `args` contract. Encode JSON inside that
string; do not put an object directly in `args`.

```json
{"assignments":[{"id":"inspect-parser","profile":"researcher","objective":"Inspect parser correctness","context":"Investigate parser.py and relevant tests","scope":["parser.py"],"dependencies":[],"acceptance":["Check malformed input handling and cite actual source"],"deliverables":["Evidence-backed findings"],"reason":"Independent specialist investigation"}]}
```

Tools: `spawn_agents`, `send_subagent`, `list_subagents`, `wait_subagents` and
`cancel_subagent`. Dependencies use batch assignment IDs or existing run IDs;
unknown dependencies and cycles fail before any job is submitted. Failed or
unverified dependencies block downstream work. Reuse requires a finished agent
and a distinct assignment.

Workers return `status` (`completed`, `partial`, `blocked`, `failed`), `summary`,
and arrays `findings`, `evidence`, `artifacts`, `checks`, `uncertainties`,
`suggested_followups`. Two JSON repair attempts are allowed, then the run fails
visibly. Model reports are kept separate from runtime-owned execution receipts
and provider usage.

Every completed or partial substantive report enters a fresh read-only review context.
Batches use reciprocal/ring peer identities; a singleton gets a reviewer. The
reviewer receives the assignment, submitted result and receipt references, and
must independently inspect evidence. A `verified` report without a successful
inspection tool receipt and supporting evidence becomes `unverifiable`.
Review verdicts are `verified`, `changes_requested`, or `unverifiable`.
Reviewers must independently read every declared artifact. A missed read gets
two bounded inspection reminders in that review context before the runtime
rejects verification; it does not immediately consume an author correction.
Inspected sources belong in `evidence`; `artifacts` describes delivered files.
Unexecuted behavioral examples must be identified as source-based reasoning.
The read-only `calculate` tool checks bounded literal arithmetic, Decimal and
pure built-in operations. It records observed expression results without
running workspace code, imports, attributes, arbitrary functions or commands.
Reviewers use it to check numeric examples; it proves the expression's behavior,
not a complete application execution. Unsupported checks remain uncertainties.
Disagreements return to the author with at most two correction cycles and fresh
rechecks. Reviews do not recursively spawn reviews.

A succeeded run means its report passed the evidence-review protocol. It does
not prove arbitrary real-world correctness. The main task still needs its
applicable acceptance checks and durable task evidence; the final synthesis
retains unresolved, failed, blocked, interrupted and cancelled work.

## Permissions and lifecycle

Children inherit the current turn's authorization and approval callbacks, then
intersect them with profile/configuration permissions. Ask/Plan children and
reviewers are read-only. Delegation cannot accept a plan or widen capabilities;
mutations during an accepted plan must match the parent's current task.
Cancellation blocks subsequent tools and dependent jobs. An already running
tool/provider request may finish; cancellation cannot undo its prior effects.
Delegated provider calls default to a 180-second deadline; foreground bounded
calls use 90 seconds. `KYROZEN_PROVIDER_TIMEOUT_SECONDS` overrides both (1–600
seconds). Reasoning reviews exceeded the earlier 90-second deadline in live
acceptance; those failures are retained in the acceptance record.
Restart marks unfinished jobs interrupted without replaying mutations.

Assignments, messages, receipts, state snapshots, results and reviews persist
with actor, project, parent-chat and run identifiers. Names are unique within
the parent chat, randomly allocated without replacement from Evan, Morning,
Joseph, Addison, Jerry, Leo, Obert, Jason, Justin, Dewey, Old Fool and Lianto, then
shuffled numbered cycles. Retries and reuse keep the name and symmetric local
identicon. After the first cycle, names use suffixes such as `Evan2`, then
`Evan3` in the next cycle. No image service or image dependency is used.

## Surfaces

The TUI activity rail shows name/icon, specialty, objective, provider/model and
state, including singleton reviewers. `/agents` or `Ctrl+E` opens inspection, including while a turn is running.
Use left/right to choose agents, arrows/PgUp/PgDn to scroll, Enter to load complete
assignment/history/receipts/reviews, `c` to cancel and Esc/q to return to the
composer. Reviewers have their own identity/icon in inspection. Snapshots and
large inspection documents stream as scoped JSONL pieces. Reduced motion and
small-terminal layout behavior follow the existing TUI settings.

Classic CLI supports `/agents`, `/agents <run-id>` and `/agents cancel <run-id>`.
The web stream shows brief lifecycle summaries.

`GET /api/v2/agents` still discovers profiles. `POST /api/v2/agents/run` retains
synchronous `profile`/`task` behavior and adds `session_id`, optional structured
`assignment`, and optional `provider`/`model`. A plain task is converted into an
assignment by the configured main provider. Inspect with
`GET /api/v2/agents/runs` and `GET /api/v2/agents/runs/{run_id}`; cancel with
`POST /api/v2/agents/runs/{run_id}/cancel`. Supply the same `session_id` query
parameter. Wrong-chat/project lookups return 404. The five orchestration tools
also appear in MCP inventory and accept `arguments: {"args":"<JSON string>"}`;
`params.session_id` selects the parent chat.

Provider/model precedence is assignment → role → main. Review calls use the
reviewer role → main. Alternate providers resolve their own provider-specific
environment credentials; generic main keys/endpoints are never reused. Missing
credentials block work explicitly. Native OpenAI Responses and Anthropic
Messages adapters are retained. Usage and cost are read from the durable ledger;
unknown usage/cost stays unknown. Configuration is documented in
[configuration.md](configuration.md).

## Acceptance evidence

`make subagent-acceptance` executes a deterministic main-agent delegation,
real tool inspections and automatic peer review. `make wheel-smoke` repeats it
against the installed wheel outside the checkout. For a configured live main
provider, run `python scripts/subagent_workflow_acceptance.py --live`.
The script isolates project/state and prints no credentials or transcripts.
That workflow request explicitly asks for specialists and cross-examination;
it verifies orchestration but does not prove automatic detection. Test ordinary
requests with `python scripts/subagent_autonomy_acceptance.py --live` instead.
Its review and single-function question mention neither agents nor verification.
It independently reproduces known defects, checks actual author/reviewer source
receipts and findings, and requires zero agents for the simple question. The
assistant's generated database/skills live outside the source project. A failed
check exits nonzero. Output contains synthetic reports and no credentials.
Use `--inject-incorrect-result` to replace one submitted finding with a known
false claim and a nonexistent artifact. Review calls remain live and independent;
acceptance requires rejection, author correction and a fresh verified recheck.
The fixture also rejects a known false positive about Decimal summation, even
when author and reviewer agree. Agreement alone cannot satisfy this oracle.

Regression coverage includes synchronization-barrier overlap, 25-agent name
rollover, independent messages/providers/usage, dependency/cycle/duplicate
handling, reuse, file ownership and opaque-command locking, cancellation,
interrupted recovery, Ask/Plan denial, malformed-report repair, broken-artifact
correction and unsuccessful-review exhaustion. Controlled HTTP transports
exercise the actual OpenAI and Anthropic SDK adapters, their distinct request
formats, credential routing and authentication failures. TUI checks exercise
scoped live events, inspection, scrolling, cancellation and terminal sizes.

These checks establish protocol and runtime behavior; they do not establish a
quality or speed advantage over another agent system. Mixed-provider live
acceptance remains BLOCKED until a second provider credential is configured
securely. Controlled provider fixtures are not mixed-provider live acceptance.

The [2026-10-01 acceptance record](subagent-acceptance.json) records PASS for all
371 Python tests (including browser/API/MCP), Go, actual TUI interaction,
architecture/documentation checks, wheel/source installation and explicitly
requested live DeepSeek delegation with independent source reads and peer review.
The [ordinary-request live record](subagent-autonomy-acceptance.json) contains a
clean installed DeepSeek experiment: three focused investigations, independent
reviews and arithmetic checks, plus a single-function answer with zero agents.
Its separate fault experiment rejected a false claim and missing artifact,
corrected the author and verified the revision. Neither user request mentions
delegation or cross-examination. Original harness exit codes are retained: an
overbroad negative-control assertion flagged genuine mixed Decimal/float errors;
the corrected oracle reassesses the unchanged native results as PASS. Earlier
runtime failures and their repairs remain in the acceptance history.
Mixed-provider live: BLOCKED. The live scripts compare author/reviewer
receipts with actual file bytes, preserving line breaks and indentation; model
agreement alone cannot pass that check. This verifies the rebuilt behavior in
the tested implementation and isolated installations. Publishing this code to
GitHub does not constitute a new package release.
