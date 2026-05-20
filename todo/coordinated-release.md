# Coordinated multi-package releases

## Context

When a foundational package makes a breaking change, downstream packages need updated dependency constraints and potentially new releases. Currently this is manual: bump the foundation, then manually update each dependent's manifest, then release in topological order.

`monorepo release-order` gives the correct sequence. `monorepo outdated` detects stale constraints. But nothing ties them into a coordinated workflow.

## Decisions

- **Dry-run all packages first**, then release. Run all tests/checks for every package in the batch before releasing any. If anything fails, nothing is released.
- **Stale internal deps as a check, not a release blocker.** After releasing a package, rlsbl prints which downstream packages have stale deps and advises next steps, but does not error or block. The stale-deps check is enforced separately via pre-push or CI.
- **Declarative release files** (open design): instead of a command, create a file describing what to release, dry-run it, then execute. Needs more design work.

## Open design threads

### Declarative release file

A file (TOML? YAML? JSONL?) that describes a coordinated release:

```toml
[[releases]]
package = "models"
bump = "major"

[[releases]]
package = "marketplace_contract"
bump = "minor"

[[releases]]
package = "flow_order"
bump = "patch"
```

Workflow: create the file, run `monorepo release --plan release.toml --dry-run` to validate everything, then `monorepo release --plan release.toml --yes` to execute.

### Multi-package tag format

Current monorepo releases use one tag per package (`package@vX.Y.Z`). For a coordinated release of N packages, that's N tags. Options:
- N individual tags (current behavior, applied sequentially)
- A compound tag encoding all versions (e.g., `batch/2026-05-20/models@v2.0.0+marketplace_contract@v1.3.0`)
- A release manifest committed alongside the tags

### Per-package bump types in batch

How does the user specify that `models` is major but `flow_order` is patch? The declarative file solves this. An interactive mode could propose bump types based on changelog entry types.

## What we need (settled)

### Stale internal dep detection

A check (via strictcli check system) that:
- For each intra-workspace dependency, reads the constraint from the consumer's manifest and the current version from the dependency's manifest
- Reports any constraint that doesn't satisfy the current version
- `monorepo outdated` already does this -- wrap it as a `@check` function

### Constraint propagation (advisory)

After releasing a package, print:
- Which downstream packages have stale deps
- What the updated constraint should be (e.g., `models: ^1.0.0 -> ^2.0.0`)
- Suggested command to update (`monorepo update-deps models` or manual instructions)

### Batch release in topological order

Given a list of packages and bump types (from declarative file or interactive selection):
1. Validate all: tests, lint, changelog for every package
2. If all pass, release in topological order (leaves first)
3. Each gets its own tag and changelog entry

## What we do NOT need

- Lockstep versioning (all packages at the same version). Each package has independent versioning.
- Automatic downstream releases. Only flag and facilitate, never auto-release.

## Prerequisites

- Unified check system in strictcli
- `monorepo outdated` refactored as a check function
- Possibly: `dep_rewrite.py` extended to update existing versioned constraints (currently only converts path deps to versioned)

## Effort

Large. The declarative release file, batch execution with dry-run-first, and constraint propagation are each significant features. The stale-deps check is small (wrapping existing `outdated` logic).
