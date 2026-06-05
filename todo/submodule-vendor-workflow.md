# Submodule vendor workflow for forked dependencies

## Problem

Projects sometimes need to fork an upstream dependency, apply patches, PR them upstream, and periodically rebase to pick up upstream changes. This is a common pattern for:
- Vendoring a tool that needs project-specific fixes
- Contributing to upstream while benefiting from fixes immediately
- Managing dependencies where the upstream is a solo maintainer (bus factor risk)

## Desired workflow

`rlsbl vendor add <upstream-url> <local-path>` should:
1. Fork the upstream repo (via `gh repo fork`)
2. Add the fork as a git submodule at the specified path
3. Add the path to the monorepo's workspace config (pnpm-workspace.yaml, etc.) if applicable
4. Track the upstream remote for rebasing

`rlsbl vendor sync <local-path>` should:
1. Fetch upstream changes
2. Rebase local patches onto upstream
3. Report conflicts if any
4. Push to the fork

`rlsbl vendor status` should:
1. Show all vendored submodules
2. How many local patches ahead of upstream
3. Whether upstream has new commits to rebase onto

## Context

Triggered by a monorepo needing to fork an upstream dependency as a submodule, apply fixes, PR upstream, and rebase periodically. The pattern is general enough to be an rlsbl feature.

## Affected projects

Any rlsbl-managed monorepo that vendors forked dependencies.
