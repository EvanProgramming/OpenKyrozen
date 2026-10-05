# Memory and local storage

OpenKyrozen distinguishes durable facts and event history from retrieval indexes. SQLite is the authoritative store for v2 sessions, events, memory claims, durable tasks, usage, schedules, and learning state. ChromaDB and Graphify data are derived indexes that can be rebuilt from their inputs; rebuilding them does not replace the SQLite source of truth.

## Files and overrides

| Data | Default or configuration | Role |
|---|---|---|
| Global workspace | `~/.kyrozen/workspace` | Default working directory for a bare installed `kyrozen` launch. |
| User state | `~/.kyrozen/v2/openkyrozen.sqlite3` | SQLite-backed durable runtime state. |
| Encrypted provider config | `~/.kyrozen_config.json` | User provider credential settings. Protect the file and its home directory. |
| Vector index | A `chroma_index_v2` sibling of the database by default; `KYROZEN_VECTOR_PATH` overrides it | Optional derived semantic-retrieval index. |
| SQLite database | `KYROZEN_DB_PATH` overrides the database path | Durable authoritative records. |
| Audit log | `KYROZEN_AUDIT_LOG` overrides the default log destination | Local security and integration audit events. |
| Server actor | `KYROZEN_SERVER_ACTOR` | Actor identity assigned to web requests; invalid values fall back to `local`. |
| Workspace root | `--project PATH` or `KYROZEN_WORKSPACE_ROOT` | Root for project-bound operations, subject to launch-context checks. |
| Legacy source-checkout memory | `./chroma_memory/` when migrating older installs | Migration input; not the active v2 vector index. |

The exact home-directory layout can evolve. Use `kyrozen --help` and the current database configuration in `openkyrozen/persistence/database.py` before writing scripts that assume internal paths. A Docker deployment sets `KYROZEN_DB_PATH=/data/openkyrozen.sqlite3` and `KYROZEN_VECTOR_PATH=/data/chroma_index`; mount the named volume at `/data` to retain state between container replacements.

## Scope and privacy

Memory claims can have owners, speakers, audiences, channels, visibility, and workspace/session scopes. Retrieval filters against those properties; multi-party records retain attribution so private statements are not generalized into shared facts. Personal memory retains a global personal scope. Project source snapshots and project history use project/session scope. A web session is actor-scoped. Do not assume deleting one conversation erases a global memory claim or learning event.

Model providers receive the context sent for a request. Local storage does not imply that model inference or browser/network operations are local. Review provider privacy terms and keep secrets and unrelated private data out of prompts. See [security](security.md).

## Backup and restore

Stop the running process before taking a simple file copy of the SQLite database. Preserve the database and its adjacent SQLite WAL/SHM files together if present; an application-level SQLite backup API or a clean shutdown is preferable to copying an actively written database file. Keep credentials, workspace files, browser data, vector indexes, and history snapshots in a backup only when your retention policy requires them, and store the backup privately.

To restore, stop OpenKyrozen, restore the database to a private location, set `KYROZEN_DB_PATH` to that file, and launch with the intended workspace. Verify the expected sessions and claims before removing the prior copy. Do not merge two live database files with a filesystem copy.

## Rebuild a derived index

If semantic retrieval fails while SQLite remains readable, first check disk permissions and the configured vector path. The agent can fall back to authoritative stored data when the vector index is unavailable. A rebuildable index can be removed and recreated after the corresponding runtime is stopped. Project Graphify indexes have their own `/graph refresh` lifecycle; rebuild a project graph only when its source project is available.

## Import legacy memory

The compatibility command accepts the source directory as an optional argument and writes to `KYROZEN_DB_PATH` or the default database:

```sh
KYROZEN_DB_PATH="$HOME/.kyrozen/v2/openkyrozen.sqlite3" \
  kyrozen migrate v1 ./chroma_memory
```

Review the migration JSON summary and preserve an original backup until the imported claims have been checked. Migration is not a merge strategy for an already-initialized destination. For implementation-level recovery evidence, read the [architecture](architecture.md) and [development guide](development.md).
