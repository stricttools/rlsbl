# Support a development branch workflow

## Problem

The pre-push hook blocks all pushes to the release branch (main) unless they go through `rlsbl release run`. For projects that iterate rapidly between releases, this forces a hotfix release for every batch of maintenance commits — even when the work isn't release-worthy yet. The alternative is accumulating commits locally and batching them into periodic releases, but this means origin/main lags during active development and other sessions can't see the work.

## Desired workflow

A development branch (e.g., `dev` or `develop`) where:
- Pushes are free (no pre-push hook gate)
- CI runs normally on push
- Merges from dev → main go through `rlsbl release run` (the release pipeline)
- The pre-push hook only gates pushes to the release branch, not the development branch

This separates "sharing work with other sessions/CI" from "cutting a release." Multiple sessions can push to dev freely; releases happen on a deliberate cadence by merging dev → main via rlsbl.

## What's needed

1. A config key in `.rlsbl/config.json` or `workspace.toml` specifying which branch is the release branch vs which is the development branch (or a pattern like `release_branch = "main"`, `development_branch = "dev"`).
2. The pre-push hook should only gate pushes to the configured release branch. Pushes to the development branch (and any feature branches) should pass freely.
3. `rlsbl release run` should work from the development branch by: merging dev → main (fast-forward or merge commit, configurable), tagging on main, pushing main.
4. Changelog coverage checks should apply to the dev→main merge range, not individual pushes to dev.

## Alternatives considered

- **Frequent hotfix releases**: works but noisy (version numbers churn for non-user-facing work).
- **Accumulate locally**: works but blocks collaboration (other sessions can't see unpushed work).
- **This proposal**: cleanest separation of development iteration from release ceremony.
