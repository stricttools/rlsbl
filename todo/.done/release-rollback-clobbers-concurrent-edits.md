# Failed-release rollback clobbers concurrent sessions' work (git reset --hard)

## Context

When `rlsbl release run` fails mid-flight, the rollback path does a blanket
`git reset --hard` back to the pre-release SHA. Multiple AI sessions routinely share
one worktree. A blanket hard reset does not just undo the release's own commits — it
destroys ANYTHING that landed in the worktree after the pre-release snapshot was
taken, including other sessions' uncommitted working-tree edits and even their
committed-but-unpushed commits.

## Incidents (two this week)

1. **Uncommitted edit clobbered:** during a failed release rollback, another
   session's in-progress uncommitted edit to `publish.py` was wiped from the working
   tree by the hard reset. The edit had to be reconstructed by hand.
2. **Committed-but-unpushed commit destroyed:** a rollback hard-reset the branch to
   the pre-release SHA, discarding a concurrent session's already-committed (but not
   yet pushed) commit adding docs directives. The branch pointer moved backwards past
   a foreign commit; the work was only recoverable via reflog archaeology.

Both incidents share the same root cause: the rollback assumes the worktree and
branch are exactly as the release left them, which is false in a shared-worktree,
multi-session environment.

## Problem statement

The rollback must be scoped to what the release itself changed, or refuse to run when
the tree/branch has moved since the release started. It must never be a blanket
`git reset --hard <pre-release-sha>`.

## Proposed solutions

### A. Snapshot-diff-based revert (most correct)

At release start, record exactly what the release will touch: the files the release
flow itself modifies (version files, CHANGELOG.md, `.rlsbl/changes/*`, release TOML)
and the commits it creates (version bump, finalize). On rollback, revert ONLY those:

- Delete/revert only the release-created commits (e.g., `git revert` them, or reset
  only if HEAD is still exactly the last release-created commit).
- Restore only the release-touched files, leaving all other working-tree state alone.

Pros: surgical, safe under concurrency, no foreign work ever touched.
Cons: most implementation work; needs a precise manifest of release-touched paths
(largely already known to the release flow).

### B. Refuse-on-foreign-commits guard (cheap, high value)

Before any rollback reset, verify:

- `HEAD` is exactly the SHA of the last commit the release itself created (i.e., no
  foreign commits landed since). If not, ABORT the rollback with a hard error telling
  the user the branch moved and manual intervention is needed.
- `git status --porcelain` is empty except for files the release itself touched. If
  foreign dirty files exist, ABORT (or fall through to a file-scoped revert that
  never touches the foreign paths).

Pros: small, immediately eliminates both incident classes; aligns with the
"hard errors, not warnings" philosophy.
Cons: rollback becomes manual in contended cases (acceptable — losing work is worse).

### C. Reflog guard / pre-rollback safety commit

Before resetting, snapshot the entire current state (e.g., create a safety ref or a
temporary commit of the dirty tree on a rescue branch) so that even if the reset is
wrong, everything is trivially recoverable without reflog archaeology.

Pros: cheap insurance, composable with A or B.
Cons: does not prevent the clobber, only makes recovery easy; rescue refs need
lifecycle management.

### Recommended combination

B now (guard + hard error), A as the proper fix, C as belt-and-braces during the
transition. The guard alone would have prevented both incidents.

## Affected files

- The release rollback/undo path in the release flow (wherever the
  `git reset --hard` to the pre-release SHA lives — release run failure handling and
  possibly `release undo`/`release retry` share this code).

## Effort

- B: small (a few pre-checks + tests simulating a foreign commit / dirty foreign file
  before rollback).
- A: medium (release-touched manifest + surgical revert + integration tests).
- C: small.

All fixes need red-green tests: a fixture repo where a foreign commit and a foreign
dirty file exist at rollback time, asserting they survive (or the rollback refuses).
