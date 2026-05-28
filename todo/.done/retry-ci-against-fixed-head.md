# Re-run CI against a fixed HEAD via release retry

## Problem

When a release ships with red CI due to a bug fixed in later commits on main, `release retry` can re-trigger CI via `gh workflow run` — but it dispatches against the tag ref by default. The tagged commit still has the bug, so CI fails again.

The user needs to re-run CI against the current HEAD (which has the fix) to prove main is green, even though the CI run won't be "associated" with the release tag's commit.

## Current state (v0.43.1)

`retry.toml` has a `ci_ref` field that defaults to the tag. The user can manually edit it to `main` or a specific SHA before running `release retry`. This works but requires knowing to edit the file.

## Possible improvements

1. **Document the ci_ref workflow**: Add guidance that editing `ci_ref = "main"` in retry.toml is the way to re-run CI against HEAD. No code change needed.

2. **Auto-detect stale CI**: When retry detects that the tag's commit has failing CI runs, suggest setting `ci_ref = "main"` in the scaffolded retry.toml.

3. **Interactive prompt**: When auto-scaffolding retry.toml, ask whether to dispatch CI against the tag or against main.

## Context

Real-world case: strictcli v0.10.0 shipped with a conformance test bug (wrong subprocess CWD). Fix landed on main two commits later. Published packages were correct — only CI conformance step failed.

## Effort

Small for option 1 (docs only). Medium for options 2-3 (CI status detection + UX).
