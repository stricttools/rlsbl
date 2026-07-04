# CI version pinning for pgdesign

## Problem

CI templates currently install pgdesign via `go install @latest`, which always fetches the newest version. This works but does not pin to a specific version, so CI behavior can change without any change to the project being built.

## Deferred approach

pgdesign stamps its version in `pgdesign.toml` under a `[tool]` section. The CI template reads the pinned version from that field and installs that exact version. pgdesign ships binary release assets (per-platform binaries attached to GitHub Releases) for fast installation without requiring a Go toolchain.

## Why deferred

The current `go install @latest` approach works and has no known correctness issues. Version pinning adds complexity (template reads TOML, binary asset URL construction, fallback handling) that is not justified until pgdesign's API stabilizes or a CI breakage occurs due to version drift.
