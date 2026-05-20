# Workspace health dashboard

## Context

`monorepo status` shows a table with version, tag, unreleased count, deps, rdeps per package. This is useful but doesn't surface problems proactively. In a 41-package monorepo, you want to know at a glance: is the workspace healthy? What needs attention?

## What we need

A `monorepo health` (or enhanced `monorepo status`) that surfaces:

- **Unreleased changes**: packages with commits since last tag, sorted by staleness.
- **Stale constraints**: packages depending on outdated versions of siblings.
- **Orphan packages**: registered in workspace.toml but directory doesn't exist (or vice versa).
- **Missing changelogs**: packages with unreleased changes but no changelog entry.
- **Cycle warnings**: if any cycles exist in the graph.
- **Layer violations**: if architectural rules are defined, which are violated.
- **Unused deps**: if import validation is available, which declared deps are unused.
- **Undeclared imports**: if import validation is available, which imports are undeclared.

A single command that runs all checks and produces a concise report. Like `rlsbl doctor` but for the workspace.

## Speculative: CI integration

This could be the single CI check for workspace health. Instead of running N separate lint checks, run `monorepo health` and fail if any critical issue is found. Warnings are reported but don't block.
