# Changelog-only commit exemption bug

## Problem

Commits that only touch `.rlsbl/changes/unreleased.jsonl` are supposed to be auto-exempted from changelog coverage validation, but they are not. The exemption logic should match any commit whose diff only touches files under `.rlsbl/changes/` or `CHANGELOG.md`, yet these commits are flagged as uncovered.

## Evidence

During the a private project v0.3.2 release, two changelog-only commits were incorrectly flagged as uncovered:

- `68dc405b` -- touched only `.rlsbl/changes/unreleased.jsonl`
- `1b067cef` -- touched only `.rlsbl/changes/unreleased.jsonl`

Both had to be manually covered with `rlsbl changelog add --no-user-facing` entries to pass validation, which defeats the purpose of the exemption.

## Expected behavior

The commit coverage check (validation check 3) should skip any commit whose `git diff-tree --no-commit-id --name-only -r <hash>` output consists entirely of paths matching:

- `.rlsbl/changes/*`
- `CHANGELOG.md`

These are "changelog-only" commits (typically auto-committed by `rlsbl changelog add`) and cannot meaningfully cover themselves -- it is a bootstrap problem.

## Where to look

The coverage check logic lives in the changelog validation code. The exemption filter likely either does not exist, uses an incorrect path pattern, or is not applied to the right set of commits.

## Effort

Small -- the fix is a predicate over `git diff-tree` output in the coverage check loop.
