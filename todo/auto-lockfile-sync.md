# Auto-lockfile sync on version bump

## Problem

After `rlsbl release` bumps the version in `pyproject.toml` (step 9), the `uv.lock` file becomes stale because it contains a hash or record of the old version. The staleness is not detected until the next `rlsbl release` attempt, which fails at step 1 with "working tree is not clean" because `uv.lock` shows as modified.

This requires a manual workaround between releases:

```bash
uv lock
safegit commit -m "sync lockfile" -- uv.lock
```

## Proposed fix

When `rlsbl release` bumps version files in step 9, it should detect lockfiles in the project and auto-sync them before committing:

- **uv**: if `uv.lock` exists, run `uv lock`
- **pip**: if `requirements.txt` was generated from `pyproject.toml`, warn
- **poetry**: if `poetry.lock` exists, run `poetry lock --no-update`

The synced lockfile should be included in the same version-bump commit (the one whose message is the tag string, e.g., `v1.2.3`).

At minimum, if auto-sync is too risky, rlsbl should warn when a known lockfile is dirty after the version bump, before committing, so the user can intervene.

## Where to look

The version bump and commit logic in the release flow (step 9-10). The release command already knows which files it modified; it would need to additionally check for lockfile staleness and run the appropriate sync command.

## Effort

Medium -- detecting lockfile types is straightforward, but running external tools (`uv lock`, `poetry lock`) introduces potential failures that need graceful handling (timeout, missing tool, lock conflict).
