# GitHub CLI

## Trigger

Use this skill for GitHub repositories, issues, pull requests, workflow runs,
releases, and repository collaboration.

## Steps

1. Use `github_read` for inspection in Ask or Plan mode.
2. Use `github_cli` only in Agent mode when the requested workflow needs the
   broader GitHub CLI surface.
3. If authentication is required, ask the user to run `/github login`; login is
   separate from approval for later mutations.
4. Review repository state before a mutation and verify the remote result after it.

## Verify

Report the GitHub CLI exit result and confirm mutations with a separate read.
Never request, display, or store a GitHub token.
