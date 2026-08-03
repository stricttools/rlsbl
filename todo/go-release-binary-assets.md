# Attach built Go binaries to GitHub Releases

Filed 2026-08-03.

## Context

Go projects in the fleet distribute via `go install` (source build from the module path) and, for local pipelines, `install_paths` in `.rlsbl/config.json`. Nothing attaches prebuilt binaries to the GitHub Releases that `rlsbl release run` already creates (`gh release create`, notes only). A planned fleet tool (a Go binary distributed registry-free via GitHub Releases) needs prebuilt per-platform binaries on its releases; the capability is generically useful for every Go project in the fleet.

## Work

- New config surface (explicit, no implicit defaults): which packages to build (`asset_paths`, analogous to `install_paths`) and which platforms (`asset_platforms`, e.g. `["linux/amd64", "linux/arm64", "darwin/arm64"]`). Both required when the feature is used; absence of the keys means no assets (explicit mode selection, not fallback).
- Release-flow step: after tag push, before/with GitHub Release creation — cross-compile each path × platform (`GOOS`/`GOARCH`, CGO off), name assets `{binary}-{version}-{os}-{arch}`, generate a checksums file, upload via `gh release upload`.
- Secret-scan the built binaries with the existing gitleaks gate before upload (same artifact-scan contract as `dist/` archives).
- Monorepo/releasable support: assets attach to the releasable's release; tag scheme via existing glob resolution.
- Failure semantics: any build or upload failure aborts the release step hard (no partial asset sets); `release retry` resumes from the failed upload; `release undo` deletes the release including assets (verify current undo covers assets).

## Options

- **A. Build in the release flow locally** (chosen shape above): consistent with the local Go pipeline model; no CI dependency. Cons: cross-compile happens on the release machine.
- **B. CI-built assets** (workflow builds and uploads after the CI gate): keeps release machine lean, but decouples asset presence from the release command's success guarantee and complicates `--watch` semantics.
- A is recommended for consistency with `local: true` Go pipelines; B can be a later variant for repos that need exotic platforms.

## Affected files

`rlsbl/commands/release/execute.py` (asset step near GitHub Release creation, `:2188-2205` region), config schema + validation, scaffold docs, `docs/release-workflow.md`.

## Effort

M.
