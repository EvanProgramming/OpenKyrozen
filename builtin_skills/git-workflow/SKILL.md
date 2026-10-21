# Git workflow

## Trigger
Use when the user requests Git operations or work requires a branch, commit, push, pull, or merge.

## Steps
1. Inspect the current branch and working tree before acting.
2. Review the exact diff before creating a commit.
3. Use a concise Conventional Commit message that describes the change.
4. Sign commits with the configured signing identity when available or required.
5. Preserve uncommitted work before switching branches.
6. Verify each requested operation and report its resulting commit or branch state.

## Verify
Do not force-push, discard changes, reset hard, or publish or merge without explicit user authorization. Never claim a Git operation succeeded until the repository confirms the result.
