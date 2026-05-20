# Impact analysis: what breaks when a package changes

## Context

In a 41-package monorepo, changing a foundational package (schema, models, infra) cascades through many dependents. Before making a change, you want to know: "if I modify models/, which packages need retesting? Which might need a release?"

rlsbl already has the dependency graph (workspace_graph.py) and can compute reverse dependencies. But there's no user-facing command that answers "what is the blast radius of changing package X?"

## What we need

Given a package name (or a set of changed files), show:

1. **Direct dependents**: packages that directly depend on the changed package.
2. **Transitive dependents**: all packages reachable via reverse dependency edges.
3. **Test scope**: which packages' test suites should run to validate the change.
4. **Release candidates**: which packages may need version bumps if the change is breaking.

This is useful for:
- CI optimization: only run tests for affected packages instead of the entire monorepo.
- Release planning: after a breaking change in models/, know exactly which packages need coordinated releases.
- Architecture review: understand the actual coupling cost of a proposed change.

## Speculative: file-level granularity

The coarse version (package-level) is useful but imprecise. A change to `models/lib/src/money.dart` only affects packages that import Money, not all packages that depend on models/. File-level or symbol-level impact analysis (via import scanning from dependency-import-validation.md) would give precise blast radius. This is much harder but much more valuable for CI optimization.
