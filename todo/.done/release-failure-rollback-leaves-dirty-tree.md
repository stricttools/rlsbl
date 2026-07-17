# Failed release rollback leaves unstaged deletions of finalized changelog files

## Context

When `release run` fails partway (observed: a hard error at the JSONL finalize step), it rolls local state back so the user can fix and retry.

## Problem

Observed in a consumer project: after a finalize-step hard error, the rollback left the working tree DIRTY — unstaged deletions of the previously released, finalized `.rlsbl/changes/<version>.jsonl` and `<version>.md` (both chmod 444, both intact at HEAD). Two issues:

1. The rollback deleted read-only, finalized changelog files from the working tree at all. Finalized version files are documented as immutable historical records; no failure path should touch them.
2. The rollback ended with a dirty tree instead of restoring it, leaving the repo in a state that blocks the documented retry path (`--no-allow-dirty`) and forces the user to manually reconcile deletions of files the tool itself removed.

Additionally, the failure message suggested resolving the situation with a `git push --force-with-lease` — advising a force-push is dangerous guidance in any automated flow and contradicts the tool-mediated-push policy.

## Solution

- Make the rollback transactional with respect to the working tree: snapshot which files the release created/renamed, and on failure restore the tree to byte-identical pre-release state (verify with `git status --porcelain` empty as a postcondition; hard-error loudly if restoration itself fails, stating exactly what is left over).
- Never delete or modify existing finalized `<version>.jsonl`/`<version>.md` files on any path, including rollback.
- Remove any force-push suggestion from error output; point to `release retry` / documented recovery instead.
- Red-green with a real-git fixture: force a finalize failure and assert the tree is clean and finalized files untouched afterwards.

## Affected files

- Release execution/rollback logic under `rlsbl/commands/release/` (finalize step and its error path)
- Real-git test fixtures

## Effort

Medium — the transactional snapshot/restore needs care around the finalize rename dance.
