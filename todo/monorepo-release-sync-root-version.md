# Monorepo release should sync root pyproject.toml version

## Problem

`rlsbl monorepo release run` bumps versions in all releasable member packages but does not update the root `pyproject.toml` version. The root package (the workspace root) stays at whatever version it was initialized with (e.g., `0.1.0`) while members are bumped to `0.7.0`.

This matters because selfdoc's `:-: var key="project.version"` reads from the root `pyproject.toml`, producing a stale version in generated documentation.

## Current behavior

- `rlsbl monorepo release run` bumps `protocols/pyproject.toml`, `scheduler/pyproject.toml`, etc.
- Root `pyproject.toml` version is untouched
- `.rlsbl-monorepo/releasables/orxtra/version` has the correct version

## Expected behavior

During a monorepo release, if the root `pyproject.toml` has a `[project].version` field, bump it to match the releasable's new version. This keeps the root package version in sync with the release version automatically.

## Workaround

A custom selfdoc directive reads from `.rlsbl-monorepo/releasables/orxtra/version` instead of `pyproject.toml`. But this only works on standalone lines due to a separate selfdoc limitation with inline directive parsing.

## Context

Discovered in orxtra (smm-h/orxtra) during documentation update. The root pyproject.toml said `0.1.0` while the project was at `v0.7.0`.
