# Handle empty remote (first push) in release flow

## Problem

When a project has a configured remote (`origin`) but the remote repository is empty (no commits have been pushed yet), `rlsbl release run` fails with:

```
Error: could not check if local branch is behind origin: Command '['git', 'rev-list', '--count', 'HEAD..origin/main']' returned non-zero exit status 128.
Cannot verify remote-ahead status. Aborting for safety.
```

The issue is in `rlsbl/commands/release.py` around line 862-873. The `git fetch origin` succeeds (the remote exists), so the code proceeds to the `rev-list` check. But `origin/main` doesn't exist because no commits have been pushed, so `rev-list` fails. The code treats this as a hard error.

## Expected behavior

If `origin/{branch}` doesn't exist after a successful fetch, that means the remote is empty and there's nothing to be behind. The check should pass (behind_count = 0) or be skipped with a note.

## Suggested fix

In the `except` block for the `rev-list` call, check if the error is because the remote branch doesn't exist (e.g., check `git branch -r` for `origin/{branch}`, or parse the git error message). If the remote branch doesn't exist, treat it as behind_count = 0 and continue.

## Affected file

`rlsbl/commands/release.py` lines 862-873

## Impact

Blocks first releases for any project that hasn't been pushed to GitHub yet. Users must work around it by manually pushing before running `rlsbl release run`, which contradicts the "never push manually" rule.
