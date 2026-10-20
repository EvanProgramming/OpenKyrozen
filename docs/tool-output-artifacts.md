# Large tool outputs

Tool results larger than 12,000 serialized characters are stored in the local
SQLite state database. The model receives a short preview and an opaque artifact
ID instead of the full result. It can call `read_tool_artifact` with that ID and
an offset to retrieve up to 1,000 characters at a time; offsets count Unicode
characters in the serialized JSON result, so pages can be concatenated exactly.

Artifacts are private to their user, workspace, and session. Knowing an ID does
not grant access from another scope. They remain available until that session or
workspace is deleted, at which point the artifact rows are deleted with the
other scoped state. Small results remain inline. The tool inventory lists the
retrieval schema and bounds.
