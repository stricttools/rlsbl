# `rlsbl check --tag changelog` crashes in monorepo context

## Problem

Running `rlsbl check --tag changelog` from any package directory in a monorepo (or from the root) fails with:

```
WorkspaceGraph.__init__() missing 1 required positional argument: 'projects'
```

This affects all 50 packages in the WWW monorepo. Discovered on rlsbl v0.41.7.

## Impact

- `rlsbl check --tag changelog` is completely broken in monorepo context
- `rlsbl release` likely also broken since it runs changelog validation (step 3 of the release flow)
- `rlsbl status` and `rlsbl unreleased` still work fine — the bug is specific to the check/validation code path

## Reproduction

```bash
cd ~/Projects/WWW/cli  # or any package dir in a monorepo
rlsbl check --tag changelog
# → WorkspaceGraph.__init__() missing 1 required positional argument: 'projects'
```

## Likely cause

A recent refactor of `WorkspaceGraph` added a required `projects` parameter to `__init__()`, but a caller in the changelog check code path still uses the old signature (no `projects` argument).

## Blocking

The WWW monorepo has 52 unreleased commits across 50 packages since v0.1.0. Changelog entries are ready to be added but release is blocked until this bug is fixed.
