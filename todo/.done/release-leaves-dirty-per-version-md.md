# Release leaves per-version .md files dirty

## Problem

`rlsbl release run` regenerates all per-version `.md` files in `.rlsbl/changes/` during the CHANGELOG.md generation step (step 14). However, only the NEW version's `.md` file and the finalized JSONL are committed. Older per-version `.md` files that were modified during regeneration are left uncommitted, resulting in a dirty working tree after a successful release.

## Reproduction

1. Have a project with several past releases (each with a `.rlsbl/changes/x.y.z.md`)
2. Run `rlsbl release run --watch --yes`
3. After the release completes successfully, run `git status --porcelain`
4. Observe: older `.rlsbl/changes/*.md` files show as modified

## Root cause

`rlsbl changelog generate` rewrites ALL per-version `.md` files (e.g., backfilling release descriptions into older versions that didn't previously have them). The finalization step (step 14) commits the new version's JSONL, the new version's `.md`, the fresh `unreleased.jsonl`, and `CHANGELOG.md`, but does not include the modified older `.md` files in that commit.

## Expected behavior

All modified `.rlsbl/changes/*.md` files should be included in the finalization commit (step 14). The working tree should be clean after a successful release.

## Observed in

selfdoc v0.16.0 release — 6 older per-version `.md` files (0.13.0 through 0.15.1) were left dirty.
