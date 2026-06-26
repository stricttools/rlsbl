# CI router exceeds GitHub Actions 50 reusable workflow limit

## Problem

`rlsbl monorepo sync` generates a single `ci-router.yml` that uses `workflow_call` to dispatch to per-package CI workflows. GitHub Actions limits a single workflow to 50 reusable workflow calls. When a monorepo has 51+ projects, the generated router silently breaks — GitHub fails with "This run likely failed because of a workflow file issue" and 0s runtime, zero jobs executed.

## Evidence

The www monorepo has 51 projects. The generated `ci-router.yml` had 51 `uses: ./.github/workflows/*-ci.yml` entries. CI failed on every push with the workflow file error. Fixed manually by extracting the 51st project (park) to standalone triggers, bringing the router to exactly 50.

## Proposed fix

When the project count exceeds 50, `_generate_router()` in `rlsbl/commands/monorepo/sync.py` should either:

1. Split into multiple router files (`ci-router-1.yml`, `ci-router-2.yml`) with ≤50 calls each
2. Convert overflow projects to standalone triggers (push/PR with path filters) automatically
3. Error with a clear message explaining the limit

## Affected files

- `rlsbl/commands/monorepo/sync.py` — `_generate_router()` function
