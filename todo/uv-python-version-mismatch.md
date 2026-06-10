# rlsbl uses system Python instead of uv-managed Python for test runner

## Problem

When rlsbl runs `uv sync` + `uv run pytest` inside a workspace member directory (e.g., `shopkeep/core/`), the resulting pytest process uses the system Python (3.14.5) instead of the project's pinned Python (3.13). This causes `ModuleNotFoundError: No module named 'asyncpg'` because asyncpg isn't installed in the system Python.

## Environment
- System Python: 3.14.5 (`/usr/bin/python3`)
- uv-managed Python: 3.13.11 (in `.venv/`)
- `.python-version` exists at workspace root with `3.13`
- rlsbl shebang: `#!/usr/bin/python3` (system Python)
- uv version: 0.9.17
- Fedora 43

## Reproduction
```bash
cd ~/Projects/shopkeep/core
rlsbl release run --watch --yes
# Tests fail: Python 3.14.5, ModuleNotFoundError: asyncpg
```

## Expected
`uv run pytest` should use the venv's Python 3.13, where asyncpg is installed.

## Workaround
Running `uv run rlsbl release run --watch --yes` from the workspace root may work (forces rlsbl itself into the uv venv). Not yet tested.

## Affected project
shopkeep (~/Projects/shopkeep/) — the Postgres migration added asyncpg as a core dependency, which isn't available in system Python 3.14.
