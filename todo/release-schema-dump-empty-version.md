# Release schema-dump writes an empty version for ldflags-versioned Go projects

## Problem

The release pipeline's strictcli schema-dump step invokes Go projects via
`go run <path> --dump-schema`. Projects whose binary version is injected at
build time through ldflags (`-X main.Version=...`) have no version value under
`go run`, so the dumped schema records `"version": ""` instead of the release
version. The committed schema file then carries an empty version on every
release, and any consumer reading the schema's version field (docs generators,
freshness checks) sees a lie.

Observed live: a Go project's committed schema carried a correct version only
because a session dumped it manually from a properly built binary; the next
release will overwrite it with `""` again.

## Solutions

1. Build the binary first (with the same ldflags the release build uses), then
   dump from the built artifact. Most correct; the dump reflects exactly what
   ships.
2. Pass the version into `go run` via ldflags in the dump invocation
   (`go run -ldflags "-X main.Version=$RLSBL_VERSION" ...`). Cheaper, but the
   ldflags spelling must match each project's variable path; the detection
   machinery for goreleaser ldflags already knows it.
3. Post-patch the dumped schema's version field the way the schema version is
   already patched elsewhere in the release flow. Smallest, but treats the
   symptom; the binary that produced the dump still reported no version.

Option 1 or 2; the ldflags path detection already exists in the Go pipeline.

## Affected

- The release pipeline's schema-dump step for Go targets.
- Any Go project with ldflags-injected versions and a committed
  `.strictcli/schema.json`.

## Effort

Small. The ldflags detection and the schema post-patch machinery both exist;
this is wiring one of them into the dump invocation plus a red-green test.
