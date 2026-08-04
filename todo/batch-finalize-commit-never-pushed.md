# Batch release finalization commits without pushing, leaving every batch repo one commit ahead

## Context

`rlsbl monorepo release run` ends with an archive gate: once every member of
the batch has been released, `_archive_batch_if_complete` calls
`_finalize_batch_file` (`rlsbl/commands/monorepo/batch_release.py`), which
renames `batch-release.toml` to a timestamped `batch-<ts>.toml`, chmods it
0o444, archives the resolved-plan sidecar under the same stem, and commits all
of it with `commit_files(...)`.

The batch flow is otherwise push-disciplined: pass 1 releases every member with
`ci-defer` (commit, never push), then ONE candidate push publishes the whole
batch, the CI gate runs on that single commit, and pass 2 tags and releases
each member on the verified candidate. Everything reaches the remote through
that one push.

The finalize commit happens *after* all of it.

## Problem

`_finalize_batch_file` commits and returns. Nothing pushes afterwards. Every
repository that has ever completed a batch release is therefore permanently one
commit ahead of its remote, carrying a `chore: finalize batch release file
(batch-<ts>.toml)` commit that never reaches origin.

Consequences observed and implied:

- `git status` reports "Your branch is ahead of 'origin/main' by 1 commit" in
  every batch-releasing repo, forever, until a human pushes something else.
- The next release's clean-tree/branch-sync preflight sees a diverged local
  branch. Whether it aborts or silently rides the stale commit into the next
  release's range depends on which check fires first.
- The commit is genuinely part of the release's record (it archives the file
  that describes the release), so leaving it local means the remote's history
  does not contain the artifact the release produced.
- The standalone release path finalizes and pushes; the batch path does not.
  The asymmetry is invisible until someone compares two repos.

## Options

**Option A -- push inside `_finalize_batch_file`.**
Add a `push_if_needed(branch, cwd=workspace_root)` after the `commit_files`
call.

- Pros: one line, at the place that creates the problem; idempotent
  (`push_if_needed` no-ops when nothing is ahead).
- Cons: puts a network operation inside a function documented as a file-rename
  helper; the caller cannot suppress it; a push failure at that point has no
  obvious remediation path since the release itself already succeeded.

**Option B -- push in the archive gate, above the finalize call.**
`_archive_batch_if_complete` already owns the "is the batch done" decision. Let
it finalize and then push, reporting the push in the batch log with the same
formatting as the candidate push.

- Pros: keeps the file helper pure; the push is visible in the flow that owns
  batch completion; failure can be reported as an unresolved step with
  remediation ("run `git push`") rather than as a release failure.
- Cons: slightly more code than A.

**Option C -- fold the finalize commit into the candidate push.**
Archive the batch file *before* the candidate push so its commit rides the one
push the batch already performs.

- Pros: no extra push at all; the batch keeps its "exactly one push" property,
  which is what makes the CI gate meaningful.
- Cons: the batch file would be archived before the batch is known to have
  succeeded -- a failed CI gate would leave the descriptor archived for a
  release that never happened, and `release resume` would have nothing to read.
  Probably wrong for that reason alone.

**Option D -- do not commit at the end; leave the archive uncommitted.**

- Pros: no ahead-of-remote state.
- Cons: violates the always-commit-generated-files convention and leaves a
  dirty tree that blocks the next release. Worse than the bug.

Recommendation: B, with the push reported as its own step so a failure is
actionable.

## Affected files

- `rlsbl/commands/monorepo/batch_release.py` -- `_finalize_batch_file`,
  `_archive_batch_if_complete`
- `rlsbl/commands/release/__init__.py` -- `push_if_needed` (the shared push
  entry point the batch already imports for the candidate push)
- Tests: `tests/test_batch_release.py`, `tests/test_batch_lock.py` (both drive
  the batch flow with a bare local remote and can assert the remote's tip)

## Effort

Small: ~10 lines plus a regression test that runs a two-member batch against a
bare remote and asserts `git rev-parse HEAD` equals `git rev-parse
origin/<branch>` after the run.
