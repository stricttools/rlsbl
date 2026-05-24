# Add poetry.lock and requirements.txt to lockfile sync

## Problem

The auto-lockfile-sync in the release flow (`_sync_lockfiles` in release.py) covers `uv.lock`, `package-lock.json`, and `go.sum` but not `poetry.lock` or `requirements.txt`. Projects using Poetry or pip-generated requirements files will have stale lockfiles after version bumps.

## What's needed

- Add `poetry.lock` via `poetry lock --no-update` to `_LOCKFILE_SPECS`
- Consider `requirements.txt` — only if generated from pyproject.toml (heuristic needed)
- Handle missing tools gracefully (poetry not installed → skip with warning)

## Effort

Small. Follow the existing pattern in `_LOCKFILE_SPECS`.
