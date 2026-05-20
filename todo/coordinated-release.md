# Coordinated multi-package releases

## Context

When a foundational package (schema, models) makes a breaking change, all downstream packages need to update their dependency constraints and potentially release new versions. Currently this is manual: bump models, then manually bump each dependent, then release in topological order.

`monorepo release-order` gives the correct sequence, and `monorepo outdated` detects stale constraints. But there's no workflow that ties them together.

## What we need

### Constraint propagation

After releasing `models@v2.0.0`, rlsbl should be able to:
- Find all packages that depend on models
- Show which have constraints that don't include v2.0.0
- Offer to update those constraints (bump the version range)

### Batch release

A way to release multiple packages in one coordinated action, respecting topological order. If schema, models, and infra all changed, release them leaves-first (schema, then models, then infra) in one workflow. Each gets its own tag and changelog entry.

### Breaking change propagation

When a package bumps its major version, downstream packages should be flagged for review. Not auto-bumped (the downstream package might not need changes), but flagged: "models went from 1.x to 2.x; these 25 packages depend on it and may need updates."

## What we do NOT need

- Lockstep versioning (all packages at the same version). Each package has independent versioning.
- Automatic downstream releases. Only flag and facilitate, never auto-release.
