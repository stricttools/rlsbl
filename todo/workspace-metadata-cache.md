# Workspace metadata cache

## Context

AI agents (Claude Code sessions) frequently need to understand a monorepo's structure: what packages exist, what each does, what depends on what, what version each is at. Currently this requires reading N pubspec.yaml files, N README files, and mentally assembling the graph. For a 41-package monorepo, this consumes significant context window and time.

Humans have a similar problem: there's no single artifact that summarizes the workspace state.

## What we need

A generated metadata cache at the workspace root that is:

1. **Plain text readable** -- an AI agent can read one file and immediately understand the entire workspace: every package, its purpose, its dependencies, its reverse dependencies, its version, its target type.
2. **Machine parseable** -- tooling, CI, and scripts can consume it programmatically. JSON or similar structured format alongside or instead of the plain text.
3. **Auto-generated** -- updated by rlsbl during every release (or on demand via a command). Never hand-edited.
4. **Committed to the repo** -- so it's available without running any commands. AI agents and new contributors see it immediately.

## What it should contain per package

- Name
- Path
- Target type (dart, python, spec, flutter-ios, flutter-android, none)
- Current version
- One-line description (from README first line, or manifest description field)
- Direct dependencies (intra-workspace only)
- Reverse dependencies (what depends on this package)
- Library flag (is it a shared library or a standalone app/tool)
- Whether it's test-only (conformance)

## What it should contain at the workspace level

- Total package count
- Leaf nodes (packages with no intra-workspace deps)
- Root nodes (packages nothing depends on, i.e., app, conformance)
- Dependency depth (longest path in the DAG)
- Topological order (release order)

## Formats

Ideally two views of the same data:
- A `.rlsbl-monorepo/cache.txt` (or similar) that's human/AI readable at a glance
- A `.rlsbl-monorepo/cache.json` that's machine parseable

Both generated from the same scan, both committed, both updated during release.

## Staleness

The cache is a snapshot. It goes stale when packages are added/removed or deps change between releases. This is acceptable if it's regenerated on every release. A `monorepo cache --refresh` command for manual regeneration would also help.
