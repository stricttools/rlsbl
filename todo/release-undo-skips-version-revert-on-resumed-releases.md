# `release undo` silently skips the version-bump revert when the tag is not on the bump commit

## Context

A resumed release tags the CI-verified commit, which after a fix-forward is NOT
the version-bump commit — later commits (changelog entries, CI fixes) sit
between them. This is the normal shape of every resumed release.

## Problem

`_walk_release_commits` (rlsbl/commands/undo.py:284-327) walks backward from
the tag's commit and stops at the first non-release-shaped subject. On a
resumed release the tag sits on e.g. a changelog commit, so the walk collects
ZERO commits, and the completeness guard (undo.py:396-440) only fires when
`revert_shas` is non-empty. Result: undo deletes the GitHub Release, the tags,
and un-finalizes the changelog — then reports success while the version files
still carry the undone version. Observed live: a monorepo releasable's undo
left `releases/<name>/version` at the undone value; repaired by hand.

## Solution

The version-bump commit is recorded in the release state / derivable from the
archived release file (it is the commit whose subject is the tag string, or the
one that wrote the version files). Locate it directly instead of walking from
the tag, and make an empty walk a HARD ERROR naming what could not be found —
never a silent "No release commits found to revert" success.

## Affected

- rlsbl/commands/undo.py (`_walk_release_commits`, the completeness guard)
- a red-green test with the resumed-release shape (tag on a post-bump commit)

## Effort

Small-medium: one lookup change plus the guard inversion plus tests.
