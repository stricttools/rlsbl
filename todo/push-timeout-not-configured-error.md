# Release rollback leaves generated files as untracked, blocking next attempt

## Problem

When `rlsbl release run` fails mid-release (e.g., at the push step), it rolls back the commit and tag. However, files generated during the release (like `core/.rlsbl/changes/0.1.0.md`) are left behind as untracked files. The next release attempt then fails with "working tree is not clean" because of these leftover generated files.

## Reproduction

1. Run `rlsbl release run --watch --yes`
2. Release proceeds: tests pass, commit created, changelog finalized, tag created
3. Push fails (e.g., push_timeout not configured, or pre-push hook rejects)
4. rlsbl rolls back commit and tag
5. `git status` shows: `?? core/.rlsbl/changes/0.1.0.md` (untracked)
6. Next `rlsbl release run` fails: "working tree is not clean"

## Expected behavior

The rollback should also clean up any files it generated during the release (the per-version .md file, any config.json changes from stale exclusion cleanup, etc.).

