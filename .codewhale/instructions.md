# OpenKyrozen Project Instructions

## Auto-commit
After every code modification session, stage and commit changes automatically
with a descriptive conventional-commit message (`feat:`, `fix:`, `refactor:`, `chore:`).

Only commit source files (`.py`, `.md`, `.txt`, `.toml`, `.yaml`, `.json`).
Do NOT commit runtime artifacts:
- `chroma_memory/` (ChromaDB persistent storage)
- `__pycache__/` and `*.pyc`
- `venv/`
- `.kyrozen_*` config files

If a `.gitignore` entry is missing for any of the above, add it first.
