# Go build_assets() only builds for host platform

## Problem

The Go target's `build_assets()` runs `go build -o <dist_dir>/ ./...` which only produces a binary for the host OS/architecture. Cross-compilation (linux/darwin/windows x amd64/arm64) is handled by goreleaser in CI via the publish workflow.

When a user runs `rlsbl release` locally with `publish.go.assets: true`, they get a single host-platform binary uploaded to the GitHub Release. This may be surprising — the CI path produces 6+ binaries.

## Options

1. Print a warning when Go build_assets runs locally: "Building for host platform only. Use goreleaser in CI for cross-compilation."
2. Integrate goreleaser locally if installed: `goreleaser build --snapshot --clean`
3. Accept the limitation and document it

## Effort

Small for option 1, medium for option 2.
