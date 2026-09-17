# dead-modules prunes every directory named build, hiding a project's own package

## Context

The `dead-modules` quality check walks a Go project's source files to find
internal packages nothing imports. Its walk prunes directory names listed in
the linter's excluded-directory set, which contains `build`, so a project's
own package at `internal/build/` is never read: every package that only
`internal/build` imports is reported dead.

## Problem

Observed on a Go project whose `internal/build/build.go` imports two other
internal packages: the check warns `internal/gen: internal package not
imported outside itself` and the same for `internal/rules/sealed`, while
`go list` shows both imported by `internal/build`. Reproduced against rlsbl
0.121.6: the source walk returns the `internal/gen` files and not
`internal/build/build.go`. A directory named `build` is an ordinary Go
package name, not only an artifact directory.

## Proposal

Prune build artifact directories by position, not by name: exclude `build/`
only at the repository root (and the other artifact names likewise), never a
directory of that name under a source tree such as `internal/` or `cmd/`.
Alternatively, drive the dead-module walk from `go list ./...` (the module's
own package list), which knows what a package is, and keep the name-based
pruning for the linters that read files rather than packages. A regression
test: a fixture module with `internal/build/build.go` importing
`internal/gen`, asserting no warning.

## Affected files

- The dead-module finder and the shared excluded-directory set under the
  linter package, and its tests.

## Effort

Small.
