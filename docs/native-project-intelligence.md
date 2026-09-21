# Native project intelligence and built-in skills

OpenKyrozen ships Graphify 0.9.64, GitHub CLI 2.101.0 integration, and
Ponytail 4.10.0 as versioned built-in skills. `/skills` reports their installed
versions and lifecycle state. They are seeded from the installed OpenKyrozen
package and change only through `/update`; local skills and Python plugins are
never replaced by that process.

## Private Graphify index

Each project is keyed by a path-derived `source_scope_id`. OpenKyrozen mirrors
only tracked, unignored, or eligible untracked source files into
`~/.kyrozen/v2/graphs/<source_scope_id>/source/`, then invokes
`python -m graphify`. Git projects use `git ls-files -co --exclude-standard`;
other projects use a bounded walk that excludes build and vendor directories.
Symlinks are not followed. Normal refreshes compare path, size, and mtime;
`/graph refresh --full` forces a clean rebuild.

Ask and Plan can use `graph_status`, `graph_query`, `graph_explain`,
`graph_path`, and `graph_refresh` because they change only OpenKyrozen's private
cache. Failed updates restore the last valid graph. Query output maps private
mirror paths back to project paths before it reaches the model or UI.

The Bubble Tea activity rail always shows a deterministic community-colored
mini-map. Press `g` or use `/graph open`; then use arrows or `hjkl` to select,
`+`/`-` to zoom, `/` to search, `Tab` to cycle communities, `Enter` for
neighbors, `p` to select path endpoints, `r` to refresh, and `Esc` to close.
Mouse clicks select and the wheel zooms.

## GitHub CLI

The installer and `/update` download the platform-specific `gh` 2.101.0
archive, verify its pinned SHA-256 digest, and atomically place the executable
under `~/.kyrozen/tools/gh/2.101.0/`. OpenKyrozen infers the GitHub hostname
from `origin`, with `github.com` as the fallback.

`github_read` exposes only allowlisted inspection commands in Ask and Plan.
`github_cli` accepts an argument array without a shell and remains Agent-only
and approval-gated. `/github login` runs `gh auth login --web`; Bubble Tea
suspends while the interactive process owns the terminal. Web and headless
flows report the exact login command instead of blocking. OpenKyrozen never
reads, copies, stores, or logs GitHub tokens.

## Ponytail

Ponytail is instruction-only: no Node hook or third-party executable plugin is
installed. Coding tasks and the coder profile default to `full`. Use
`/ponytail off|lite|full|ultra` to persist a scoped preference. Informational
Ask turns are unaffected, and safety, validation, accessibility, and acceptance
requirements always take priority.
