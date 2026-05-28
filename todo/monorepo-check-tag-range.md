# rlsbl check uses wrong tag range in monorepos

## Problem

`rlsbl check --tag changelog` uses the wrong tag to determine the unreleased commit range in monorepo sub-projects. It appears to use `git describe --tags --abbrev=0 --match 'v*'` (or similar), which finds old non-monorepo tags like `v0.1.1` instead of the sub-project's monorepo tag (e.g., `go/v0.6.0`).

`rlsbl status` correctly uses the monorepo tag — it shows "12 commits ahead of go/v0.6.0". But `rlsbl check` shows 56 uncovered go-touching commits, because its range starts from `v0.1.1` (674 commits ago).

`rlsbl release run` also uses the correct range (releases pass validation). Only `rlsbl check` has the discrepancy.

## Observed behavior

```
$ cd go && rlsbl status
JSONL:     3/12 commits covered (28 exempted)
! 12 commits ahead of go/v0.6.0

$ cd go && rlsbl check --tag changelog
FAIL  changelog-coverage         56 uncovered commit(s)
        skipped 581 commit(s) outside package directory  # scoping works
        # but range is too wide — 56 go-touching commits from v0.1.1..HEAD
```

The 0.43.0 directory scoping fix correctly filters to go/-touching commits, but the range is still wrong, so old go-touching commits from before go/v0.6.0 appear as uncovered.

## Expected behavior

`rlsbl check --tag changelog` in a monorepo sub-project should use the sub-project's monorepo tag pattern (e.g., `go/v*`) to determine the range, matching what `rlsbl status` and `rlsbl release run` do.

## Effort

Small. The fix is to use the same tag-discovery logic that `status` and `release run` use, instead of the generic `v*` pattern.
