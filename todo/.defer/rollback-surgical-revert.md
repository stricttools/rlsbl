# Surgical revert for release rollback

## Problem

When a release fails mid-flow, the rollback uses `git reset --hard pre_release_sha`. Guard B (shipped in v0.102.0) refuses to roll back when foreign commits exist, requiring manual intervention. This todo would make rollback work automatically even with foreign commits present.

## Proposed solution

Replace `git reset --hard` with a file-level revert that only undoes files the release touched (using the `release_commits` list from the state file). Foreign commits survive.

## Why deferred

Guard B is sufficient. The concurrent-session scenario is rare, the guard catches it, and manual recovery is straightforward (the error message includes step-by-step instructions). The surgical revert adds complexity for marginal improvement:

- File-level conflicts (release + foreign commit touching the same file) have no clean resolution — either clobber the foreign change or leave a partial rollback.
- Release commits (version bump, changelog finalize) would remain in history with their content reverted, creating a confusing log.
- The guard's "refuse and explain" approach is more honest than attempting partial automation.

## Revisit when

Guard B fires frequently enough that manual recovery becomes a pain point.
