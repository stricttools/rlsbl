# goreleaser is broken for all monorepo Go projects

## Context

The Go publish workflow template scaffolds a goreleaser job that runs
`goreleaser release --clean` on the release tag. goreleaser derives the
version from the current git tag and requires it to parse as semver
(optionally with a `v` prefix).

## Problem

Monorepo releases tag with a project-prefixed format — `name@vX.Y.Z` or a
path-style prefix like `go/vX.Y.Z`. Neither parses as semver, so the
goreleaser job fails on every monorepo Go release with:

```
failed to parse tag '<prefix>/vX.Y.Z' as semver
```

This affects EVERY monorepo Go project that publishes binaries via the
scaffolded goreleaser job. Standalone Go projects (plain `vX.Y.Z` tags) are
unaffected.

## Solutions

1. **Tag-format fix (required either way).** Two options:
   - Set `GORELEASER_CURRENT_TAG` in the workflow env, stripping the
     monorepo prefix from `$GITHUB_REF_NAME` before invoking goreleaser.
     Pro: no `.goreleaser.yaml` changes. Con: shell string surgery in the
     workflow template; must know the prefix.
   - Use goreleaser's monorepo support (`monorepo.tag_prefix` and
     `monorepo.dir` in `.goreleaser.yaml`), which tells goreleaser to strip
     the prefix itself. Pro: goreleaser-native, declarative. Con: requires
     rlsbl to template/scaffold `.goreleaser.yaml` with the correct prefix
     per project (note: goreleaser's tag_prefix support historically assumed
     `{prefix}/vX.Y.Z`-style tags — verify it handles `name@vX.Y.Z` too).

2. **is_library conditional (also needed).** Libraries consumed via
   `go get` need NO goreleaser job at all — tagged commits are directly
   installable. The template should only emit the goreleaser job for
   projects that ship binaries (e.g., gate on `install_paths` presence or an
   explicit library flag), while always keeping the gate job (the root
   publish router hard-errors on tags matching no known prefix, so a
   gate-only workflow must remain even for libraries).

## Affected files

- The Go publish workflow template (goreleaser job emission)
- Monorepo sync/router generation (must keep emitting the gate for
  library projects)
- Possibly `.goreleaser.yaml` scaffolding if option 1b is chosen

## Effort estimate

Small-to-medium: template conditional + tag handling + a test that renders
the Go publish workflow for (a) a monorepo binary project, (b) a monorepo
library, (c) a standalone project, and asserts the goreleaser job and tag
handling are correct in each.
