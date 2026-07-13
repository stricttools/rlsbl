# Push-timeout rollback destroys the state that `release resume` needs

## Context

During a monorepo releasable release, the mutating phase completed through TAGGED
(VERSION_BUMPED, COMMITTED, CHANGELOG_FINALIZED, RELEASE_FILE_FINALIZED, TAGGED),
then the `git push` step hit the 120s default `RLSBL_PUSH_TIMEOUT` on a transient
network stall (payload was ~0.05 MiB; the remote was verifiably reachable seconds
later; origin never moved).

## Problem

The timeout triggered the release rollback path, which:

1. **Deleted `in-progress.json`** — making `rlsbl release resume` unusable for the
   exact failure class it exists to recover from. A push failure after TAGGED is
   the canonical resumable state (everything local is consistent; only the push
   remains), yet the rollback erases the resume state instead of recording a
   failed PUSHED step.
2. **Deleted the local release tag** — which then had to be recreated by hand at
   the correct commit (the post-finalize HEAD, not the version-bump commit; this
   is non-obvious and easy to get wrong during manual recovery).
3. **Deleted committed files from the working tree** — the finalized
   `<version>.jsonl` and `<version>.md` changelog files (already committed in the
   finalize commit) were removed from disk, leaving a dirty tree with ` D` entries.
   This is another instance of the rollback-scope-too-broad class.

Manual recovery required: restoring the two files from HEAD, re-creating the tag
at the right commit, hand-reconstructing `in-progress.json` against the schema in
`commands/release/execute.py` / `release_state.py`, and re-running
`RLSBL_PUSH_TIMEOUT=300 rlsbl release resume --watch --yes` — which then succeeded
first try, confirming the failure was transient and fully resumable all along.

## Expected behavior

A push failure (timeout or rejection) after TAGGED should be classified as a
**resumable failure**, not a rollback trigger:

- Keep `in-progress.json`; record PUSHED in `failed_steps` with the error.
- Keep the local tag and all finalize commits (they are consistent local state).
- Print the exact resume command (and suggest raising `RLSBL_PUSH_TIMEOUT` when
  the failure was a timeout).
- Rollback should be reserved for failures *before* the local state is coherent —
  and even then must never remove committed files from the working tree.

## Possible solutions

1. **Classify push failures as resumable** (keep state, mark failed step, no
   rollback). Smallest fix, matches the existing state model's intent.
2. **Narrow rollback scope** so it never deletes the state file, tags, or
   committed files — rollback only reverts uncommitted mutations.
3. Both of the above; the worktree-isolated-release work being designed would
   subsume the committed-file deletion but not the state/tag destruction.

## Affected

- `commands/release/execute.py` (push step error handling + rollback path)
- `commands/release/release_state.py` (state lifecycle)
- Tag deletion in the rollback/error handler

## Effort

S–M. The classification change is small; tests for timeout-vs-rejection paths and
a resume-after-push-timeout integration test are most of the work.
