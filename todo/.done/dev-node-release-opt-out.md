# Dev nodes should be excludable from monorepo release

## Problem

`rlsbl monorepo release` releases every workspace project, including dev_node projects. Dev nodes are test infrastructure with no consumers — their version numbers, tags, and GitHub Releases add zero value. They also add friction: a partial release failure on a dev node (e.g., pre-push hook error during tag creation) can leave the repo in a diverged state that requires manual recovery, all for a release nobody uses.

Currently `dev_node = true` only exempts projects from changelog infrastructure. They still get version bumps, tags, GitHub Releases, and participate in the batch release flow.

## Proposed solution

Add a `release = false` option (or derive it from `dev_node = true`) in `workspace.toml` that excludes a project from `monorepo release` entirely. Dev nodes with `release = false` would:

- Not be included in the release order
- Not get version bumps, tags, or GitHub Releases
- Not block the batch release if they have issues
- Still be testable via pre-release hooks (conformance tests run as part of the other projects' releases)

## Effort

Small. Filter out `release = false` projects from the batch release list in `monorepo release`. The version/tag/release machinery already runs per-project — just skip the ones opted out.
