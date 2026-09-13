# `rlsbl release yank` appends its GitHub Release notice on every run

## Context

In a workspace whose releasable spans several members with different targets,
`rlsbl release yank <version>` has to be run once per member directory,
because from the workspace root it resolves the wrong tag spelling and errors
with "GitHub Release not found", and each member run performs only that
member's registry action.

## Problem

Every run also rewrites the shared GitHub Release: it sets the pre-release
flag and prepends the yank notice without checking whether the notice is
already there. Two member runs left two identical `> **Yanked:** ...` lines at
the top of the Release body; the duplicate had to be removed by hand with
`gh release edit --notes-file`.

## Solutions

1. Make the Release edit idempotent: detect an existing yank notice for the
   same version and reason and leave the body unchanged. Red-green with a fake
   `gh` returning a body that already carries the notice. Recommended.
2. Scope the command to the releasable so one invocation performs every
   member's registry action and edits the Release once. Larger, and the right
   long-term shape; 1 is still worth having underneath it.

## Affected files

`rlsbl/commands/release/yank.py` (or wherever the yank and deprecate notices
are written; `deprecate` likely shares the defect), their tests.

## Effort

Small for 1.
