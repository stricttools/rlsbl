# Go local install detection fails for non-main.go entry points

## Problem

The Go pipeline's `_detect_install_path()` in `rlsbl/pipelines/go.py` only finds CLI binaries by looking for files literally named `main.go` (via `glob.glob("cmd/*/main.go")` or checking for a root `main.go`). Go does not require `main.go` as a filename — any file with `package main` and `func main()` is a valid entry point.

Projects that use a different filename (e.g., `cmd/pgdesign/cli.go`) have their local install silently skipped during `rlsbl release run`. The proxy notification works, but `go install` never runs because `_detect_install_path()` returns `None`.

## Impact

The `"local": true` config option in the Go pipeline has no effect for these projects. The binary stays stale after release.

## Proposed fix

Change `_detect_install_path()` to scan for any `.go` file in `cmd/*/` directories that contains `package main`, not just `main.go`. Similarly, `dev_install_command()` in `targets/go.py` should use `go install ./cmd/<detected>` rather than `go install ./...` (which fails when the root package is not `package main`).

## Affected projects

Any Go project where the CLI entry point is not named `main.go`.
