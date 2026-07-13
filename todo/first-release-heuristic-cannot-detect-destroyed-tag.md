# First-release heuristic misfires when a version's tag was destroyed

## Context

`release run` has a first-release path: if the current version has never been tagged, it publishes the version as-is and ignores the bump type. The check is tag-existence only.

## Problem

"Never tagged" and "tag existed but was deleted" are indistinguishable to this heuristic. Observed in a consumer project after an undo incident deleted tags: version files said X.Y.Z, `.rlsbl/changes/X.Y.Z.jsonl` existed (finalized, read-only), CHANGELOG.md contained the X.Y.Z section — yet `release run` confidently entered the first-release path ("First release: releasing X.Y.Z as-is"), ran the full pipeline (checks, tests, secret scan, version-bump commit), and only THEN hard-errored at the finalize step ("refusing to finalize changelog for X.Y.Z: `.rlsbl/changes/X.Y.Z.jsonl` already exists... remove it manually"), triggering a rollback (which has its own bug — see the rollback todo).

The contradiction was knowable before any work happened: a finalized JSONL for the exact version being "first-released" is proof the version is not new.

## Solution

Preflight consistency check, before the pipeline runs: if the current version has no tag BUT `.rlsbl/changes/<version>.jsonl` exists (or CHANGELOG.md contains a section for it), hard-error immediately with a diagnosis ("version X.Y.Z appears to have been released before — its finalized changelog exists — but no tag vX.Y.Z is present; the tag may have been deleted") and enumerate the recovery options (restore the tag, or move the version forward). Fail fast, do nothing destructive, roll nothing back.

Red-green: real-git fixture with a finalized `<version>.jsonl` and no tag; assert `release run` exits non-zero before creating any commit.

## Affected files

- The first-release detection in `rlsbl/commands/release/` (version/tag resolution, preflight)
- Tests

## Effort

Small-medium — the check itself is cheap; the value is in the error message quality.
