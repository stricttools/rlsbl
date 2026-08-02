# goreleaser publish job incompatible with prefixed monorepo tags

## Problem

The generated publish job for a Go/goreleaser releasable in a monorepo cannot
handle prefixed monorepo tags (e.g. `<name>@vX.Y.Z`). rlsbl creates release
tags with a `{name}` prefix, but OSS goreleaser expects bare semver tags and
has no `monorepo:` block (that is a goreleaser-Pro feature). The generated job
delegates publishing to goreleaser's own publisher, which breaks in two ways.

## Empirical findings (from a real first release)

1. **Validation failure.** The documented workaround is to strip the prefix and
   hand goreleaser the bare `vX.Y.Z` via `GORELEASER_CURRENT_TAG`. But that
   names a git tag that DOES NOT EXIST — the real tag is the prefixed
   `<name>@vX.Y.Z`. goreleaser's default tag validation then fails hard with:

   ```
   git tag vX.Y.Z was not made against commit <sha>
   ```

2. **Wrong release target even with validation skipped.** Adding
   `--skip=validate` gets past (1), but goreleaser's release step still performs
   its release-lookup / creation using the resolved (stripped) name `vX.Y.Z`.
   With `release: mode: append` it targets a Release named `vX.Y.Z`, NOT the
   prefixed `<name>@vX.Y.Z` Release that rlsbl already created. So assets would
   land on the wrong (or a newly-created bare) Release, and the prefixed Release
   the launcher shims download from stays empty. The shims reconstruct asset
   URLs under the prefixed tag, so a bare-tag upload is invisible to consumers.

## Observed one-time workaround (stopgap, not a fix)

A manual LOCAL asset build, then attach to the existing prefixed Release:

```bash
# in the go project dir
GORELEASER_CURRENT_TAG=vX.Y.Z goreleaser release --skip=validate,publish,announce --clean
gh release upload "<name>@vX.Y.Z" dist/<archives...> dist/checksums.txt --repo <owner>/<repo>
```

This produced exactly the default-named archives
(`<project>_<ver>_<os>_<arch>.tar.gz`, `.zip` on windows) plus `checksums.txt`
and attached them to the prefixed Release. It is a manual stopgap; every future
release would need the same hand-work.

## Workable directions (pick one; the last is the most robust)

1. `goreleaser release --skip=validate` PLUS explicit targeting of the prefixed
   existing Release for uploads — IF goreleaser exposes a way to direct uploads
   to a specific Release by tag name (investigate; may not be supported in OSS).
2. A pre-step in the runner that creates a LIGHTWEIGHT LOCAL-ONLY git tag
   `vX.Y.Z` at the release commit (never pushed). This satisfies goreleaser's
   tag validation locally, but the release-lookup still resolves to the bare
   `vX.Y.Z` name, so it fixes (1) only, not the wrong-release-target problem (2).
3. **rlsbl emits its own asset-build + upload steps** instead of delegating to
   goreleaser's publisher: build with `goreleaser release --skip=publish`
   (or `goreleaser build`) to produce `dist/`, then
   `gh release upload "<name>@vX.Y.Z" dist/*` against the prefixed Release.
   This sidesteps goreleaser's tag parsing AND its release-lookup entirely and
   is the most correct fix — the generated job becomes prefix-aware natively and
   needs no manual re-apply.

## Where this is generated in rlsbl

The Go publish job is inlined into the publish router by `rlsbl monorepo sync`
from the per-project Go publish workflow. rlsbl does NOT currently emit the
prefix-strip step, `--skip=validate`, or prefixed-release targeting at all —
consumers hand-edit the generated router and must RE-APPLY after every
`rlsbl monorepo sync` / `scaffold` (which regenerates and drops manual edits).
The generator should emit prefix-aware goreleaser handling whenever a
releasable's `tag_format` contains a `{name}` prefix.

## Empirical note

This surfaced on the first goreleaser-shipping monorepo releasable in the
ecosystem, on its first release. Both symptoms (validation failure, then
wrong-release-target after skipping validation) were observed empirically.

## Red-green expectation

1. RED: add a test that renders the monorepo publish router for a Go releasable
   whose `tag_format` includes a `{name}` prefix, and assert the goreleaser job
   (a) strips the prefix into `GORELEASER_CURRENT_TAG`, (b) passes
   `--skip=validate`, and (c) targets the PREFIXED release for uploads (or uses
   rlsbl's own `gh release upload <prefixed-tag>` steps). Verify it fails
   against the current generator.
2. GREEN: implement the chosen direction; verify the test passes.

## Effort

Medium — needs a design choice among the directions above (direction 3 is the
most robust), a generator change keyed on prefixed `tag_format`, and a
router-render test.
