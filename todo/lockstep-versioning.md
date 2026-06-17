# Lockstep versioning for monorepo workspaces

## Problem

Monorepo workspaces with many packages (40+) that are all private and consumed only internally have no reason for independent versioning. The per-package version/changelog/release overhead scales linearly with package count and dominates every release session.

The current workaround — "always put all packages in the batch release file at the same bump" — is a convention, not a constraint. Nothing prevents a single-package release from drifting versions. This violates the hard-errors-not-soft-guidance principle.

## Proposed feature

A `lockstep = true` flag in `workspace.toml` that enforces all packages share one version.

### Behavior when lockstep is enabled

- **One version.** All packages read their version from a single source (the workspace root's version file). Per-package version fields either don't exist or are auto-synced.
- **Individual releases blocked.** `rlsbl release run` from a package directory errors with "lockstep workspace — use rlsbl monorepo release."
- **Batch release is all-or-nothing.** `rlsbl monorepo release` releases all non-dev_node packages together. No per-package selection in the release file — the only field is the bump type (and description/context).
- **One user-facing changelog.** The root `unreleased.jsonl` is the single source of truth for user-facing entries. Per-package `unreleased.jsonl` files hold only auto-generated non-user-facing entries (scoped to commits touching that package's directory).
- **Changelog coverage scoped per package.** Each package still requires coverage for commits touching its files (directory scoping from 0.43.0). But the user only writes entries in the root changelog — per-package coverage is satisfied by auto-generated non-user-facing entries during `monorepo release`.
- **One tag.** Tag format is `v0.3.0`, not `cli@v0.3.0`. One GitHub Release with the root changelog.

### What survives from monorepo mode

- `workspace.toml` with all project entries, `depends_on`, `library`, `dev_node`, `watch` patterns
- Layer definitions and `layers-violations` check
- Per-package CI (path-filtered via ci-router)
- Per-package test/lint during release
- `deps-unused`, `deps-undeclared`, `dead-workspace-packages` checks
- `monorepo impact`, `monorepo graph`, `monorepo status`

### What changes

- `monorepo release-order` becomes irrelevant (everything releases at once)
- `monorepo release-init` scaffolds a simple file with just `bump` and `description`
- Per-package `.rlsbl/changes/` directories hold auto-generated non-user-facing entries only
- `rlsbl status` shows one version for the workspace, not a per-package table
- Tags are plain `v*`, not `name@v*`

## Context

Discovered in the WWW monorepo (45 Python packages, 2 dev_node, all private, 1 external consumer being extracted to its own repo). A release session that should have taken 15 minutes took hours fighting per-package changelog coverage, bootstrap loops (new commits need entries which create new commits), and batch coordination. The monorepo infrastructure (layers, dependency validation, import scanning) is valuable — the per-package versioning is not.
