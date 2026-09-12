# Scaffold's additive .gitignore merge appends an unanchored `build/` and then untracks a Go package directory

## Context

`rlsbl scaffold` merges its `.gitignore` template additively into the project's
file and then runs an untrack step over paths the merged file now ignores. The
template (the base stored at `.rlsbl/bases/.gitignore`) carries the pattern
`build/` without a leading slash, meant for a build-artifact directory at the
repository root.

## Problem

An unanchored `build/` matches any directory named `build` at any depth. A Go
project with a package at `internal/build/` had every tracked file under it
removed from the index by the untrack step, in a commit the scaffold made. The
project already ignored its artifact directory with an anchored `/build/` and a
comment recording this exact hazard; the additive merge re-added the unanchored
form beside it. The untrack step trusts the merged patterns without asking
whether a newly ignored path is currently tracked source.

Restoring the files is easy once noticed; the hazard is that the untrack commit
looks routine and the next `rlsbl scaffold` repeats it.

## Solutions

1. Anchor every directory pattern in the template (`/build/`, `/dist/`, and the
   rest) so a pattern names a root path, never a name at any depth. Red-green:
   a fixture with a tracked `internal/build/x.go` must keep it tracked across a
   scaffold. Recommended.
2. Make the untrack step refuse, listing the paths, when a merged pattern would
   untrack a file that is tracked and not a known artifact; the operator then
   fixes the pattern. Complements 1.
3. Skip the additive merge when the project's file already covers the template's
   intent under an anchored spelling. Fragile; not recommended on its own.

## Affected files

The `.gitignore` template and its merge, the post-merge untrack step, and the
scaffold tests.

## Effort

Small for 1 and 2 together.
