# Monorepo: changelog coverage should be scoped to the releasing sub-project

## Problem

When releasing a sub-project in a monorepo, `rlsbl changelog validate` checks that ALL commits since the last tag have JSONL entries — including commits that only touched other sub-projects. This forces authors to bulk-add non-user-facing entries for every cross-project commit, which is tedious busywork that produces no value.

For example, when releasing `python/` at `strictcli@v0.6.0`, the coverage check requires entries for commits that only modified `go/` or `conformance/` files. These commits are irrelevant to the Python release.

## Expected behavior

Coverage should only require entries for commits that modified files within the releasing sub-project's path (as defined in `workspace.toml`). Commits that exclusively touched other sub-projects should be automatically exempted.

The workspace already has the path information:

```toml
[[projects]]
path = "python/"
name = "strictcli"
```

A commit that only modifies files outside `python/` should be auto-exempted when validating the `strictcli` sub-project's changelog.

## Impact

Every monorepo release currently requires bulk-adding dozens of non-user-facing entries for unrelated commits. In strictcli, this routinely means covering 50-170 commits that have nothing to do with the sub-project being released.
