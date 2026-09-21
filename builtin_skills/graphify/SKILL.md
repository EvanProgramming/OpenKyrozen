# Graphify project intelligence

## Trigger

Use this skill for codebase questions, architecture work, dependency tracing,
and implementation tasks involving the active project.

## Steps

1. Query the private project graph before broad file searches or repeated reads.
2. Use `graph_explain` for one symbol and `graph_path` for a dependency chain.
3. Read exact project files only after the graph identifies relevant locations.
4. Refresh the graph when results are stale or known Git operations changed files.
5. Treat inferred edges as leads and verify consequential claims in source.

## Verify

Confirm important claims against graph source locations and the smallest relevant
set of source files. Never treat a missing graph edge as proof that code does not
exist.
