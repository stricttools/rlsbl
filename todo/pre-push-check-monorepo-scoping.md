# pre-push-check doesn't use monorepo directory scoping

## Problem

`rlsbl pre-push-check` in a monorepo requires every pushed commit to have a JSONL changelog entry in EVERY sub-project, regardless of which files the commit touches. The 0.43.0 fix added directory scoping to `rlsbl check --tag changelog` ("skipped N commit(s) outside package directory"), but `pre-push-check` uses a different code path that doesn't scope.

## Reproduction

```bash
# Commit only touches python/ files
git commit -m "feat: checks_path" -- python/strictcli/__init__.py

# Pre-push hook fails:
# conformance: JSONL changelog missing coverage for 1 commit(s): c7ee8c1
# Even though c7ee8c1 doesn't touch conformance/ at all
```

## Impact

Every cross-project commit must be manually added to every sub-project's JSONL as a non-user-facing entry, defeating the purpose of the directory scoping fix. This creates cascading coverage failures during releases.

## Expected behavior

`rlsbl pre-push-check` should skip commits that don't touch the sub-project's directory, matching the behavior of `rlsbl check --tag changelog` with the 0.43.0 scoping fix.
