# Pre-push hook should block manual pushes

## Problem

The pre-push hook (`rlsbl pre-push-check`) verifies JSONL changelog coverage but doesn't distinguish between pushes from `rlsbl release` and manual `git push`. The "never push manually" rule exists in documentation (CLAUDE.md) but isn't enforced at the git level.

A user (or AI agent) running `git push origin main` bypasses the release flow entirely. The hook passes as long as changelog coverage is satisfied, which it may be (the commits might already have entries from a prior backfill).

## Proposed behavior

The pre-push hook should reject pushes that aren't from `rlsbl release`:

1. **Detection**: `rlsbl release` could set a short-lived marker (e.g., a temp file like `.rlsbl/.pushing` or an env var `RLSBL_RELEASE_PUSH=1`) before invoking `git push`. The pre-push hook checks for this marker.

2. **Without the marker**: the hook prints a warning and blocks:
   ```
   ERROR: Manual push blocked. Use `rlsbl release` to push.
   If this is intentional, use `git push --no-verify` (not recommended).
   ```

3. **Opt-out**: Projects that don't want this strictness can set `"block_manual_push": false` in `.rlsbl/config.json`.

## Edge cases

- Force pushes (`--force`): should these also be blocked? Probably yes, with explicit `--no-verify` escape hatch.
- Pushes to non-main branches: should be allowed (feature branches, WIP). Only block pushes to the release branch (typically `main`).
- `rlsbl undo` says "you must `git push` manually after" — this is a legitimate case. The hook could detect that HEAD is behind remote (revert scenario) and allow it, or `rlsbl undo` could set the same marker.

## Context

Observed in a private project: after `rlsbl release` succeeded, additional commits were created (npm wrapper). Instead of running `rlsbl release patch`, a manual `git push` was done. The pre-push hook didn't block it because changelog coverage was satisfied.
