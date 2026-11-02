# Large tool outputs

Tool results larger than 12,000 serialized characters are stored in the local
SQLite state database. The model receives a short preview and an opaque artifact
ID instead of the full result. It can call `read_tool_artifact` with that ID and
an offset to retrieve up to 1,000 characters at a time; offsets count Unicode
characters in the serialized JSON result, so pages can be concatenated exactly.

Artifacts are private to their user, workspace, and session. Knowing an ID does
not grant access from another scope. They remain available until that session or
workspace is deleted, at which point the artifact rows are deleted with the
other scoped state. The most recent native tool round remains inline. Before a
later tool round is sent to the provider, older native replies become artifact references
while their call/reply identities remain intact. The tool inventory lists the
retrieval schema and bounds. Access still requires permission to use
`read_tool_artifact`.

Legacy text-protocol turns also replace earlier rounds with scoped artifact
references. Their reference includes an `Action` example whose `args` is a JSON
string containing `artifact_id`, `offset`, and `limit`. Archived rounds contain
structured results, including content beyond the legacy display limit. The newest
round stays inline, and a storage failure stops the turn before another model request.
