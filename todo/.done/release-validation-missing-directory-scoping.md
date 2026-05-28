# Release validation doesn't apply directory scoping

## Problem

`rlsbl check --tag changelog` correctly applies directory scoping (added in 0.43.0), but `rlsbl release run` and `rlsbl monorepo release` do NOT. The release validation path still runs unscoped changelog checks, causing every package release to fail in monorepos.

## Symptoms

From a monorepo package directory:
- `rlsbl check --tag changelog` → PASS (correctly scopes to package directory)
- `rlsbl release run --dry-run --yes --allow-dirty --no-watch` → FAIL with 57 uncovered commits (runs unscoped check, sees all commits regardless of directory)

Same happens with `rlsbl monorepo release` — it validates with the unscoped path and fails.

## Impact

**Blocks all releases** in monorepos that have commits touching files outside any given package's directory. In the WWW monorepo (50 packages), no package can be released because the release validation sees all 57 unreleased commits and expects each package to cover all of them.

## Root cause

The 0.43.0 directory scoping fix was applied to the `check` command codepath but not to the release validation codepath. These two paths need to share the same scoped validation logic.

## Reproduction

```bash
cd ~/Projects/WWW/cloudflare
rlsbl check --tag changelog          # PASS
rlsbl release run --dry-run --yes --allow-dirty --no-watch  # FAIL
```

## Expected behavior

`rlsbl release run` should use the same directory-scoped validation as `rlsbl check --tag changelog`. A cloudflare package release should only require changelog entries for commits touching `cloudflare/**`, not every commit in the repo.
