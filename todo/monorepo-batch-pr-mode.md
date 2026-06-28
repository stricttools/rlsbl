# Monorepo batch PR mode

## Problem

When `release.mode = "pr"` in a monorepo, `rlsbl monorepo release run` currently iterates packages and calls the single-package release flow per package. In PR mode, each call would create its own `release/v{version}` branch and PR. This creates N PRs for N packages instead of one coordinated PR.

## Desired behavior

A single release branch (e.g., `release/batch-YYYYMMDD`) containing version bumps, changelog finalization, and release file finalization for ALL packages in the batch. One PR with a body listing all packages being released. One `pending.json` containing an array of release entries.

## Why this is complex

1. **Branch naming**: The current scheme is `release/v{version}` per package. A batch needs a different name like `release/batch-{date}` or `release/batch-{hash}`.

2. **Single-package release flow changes**: `_run_release_mutating` in `execute.py` creates the branch, does mutations, writes pending.json, pushes, creates PR, and switches back. In batch mode, all of these steps need to be deferred or shared across packages. The branch creation and PR creation need to happen once (in the batch orchestrator), while version bumps and changelog finalization happen per package (on the shared branch).

3. **pending.json schema change**: Currently a single object. Needs to become an array of entries, one per package/releasable.

4. **Finalize workflow**: The CI workflow that runs after PR merge needs to iterate the array and create tags/GitHub Releases for each package.

5. **Undo**: PR-mode undo for a batch release needs to close ONE PR and clean up ONE branch, but restore state for ALL packages.

## Potential approach

- Add a `batch-pr-mode` flag to the release flow that tells `_run_release_mutating` to skip branch creation, pending.json writing, push, and PR creation (do only version bump + changelog).
- The batch orchestrator creates the branch, iterates packages (each doing version bump + changelog on the shared branch), writes a batch pending.json (array), pushes, and creates one PR.
- The finalize workflow detects array vs object in pending.json and handles accordingly.

## Effort

Medium-high. Requires changes to `execute.py`, `batch_release.py`, the finalize workflow template, and batch undo logic. Should be done as a dedicated feature, not squeezed into the PR-mode foundation work.

## Affected files

- `rlsbl/commands/monorepo/batch_release.py` (orchestrator changes)
- `rlsbl/commands/release/execute.py` (batch-pr-mode flag, skip branch/PR steps)
- `.rlsbl/templates/` (finalize workflow changes)
- `rlsbl/commands/undo.py` (batch PR undo)
- `tests/test_pr_release.py` (batch PR tests)
