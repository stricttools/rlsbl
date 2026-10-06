# Stamp each built artifact with its install channel, and enforce data-migration step rules at release

## Context

strictcli is designed to gain a framework-owned `self` command group:
`<app> self update --to <version|latest>` installs a new binary and then runs
the data migration steps the app registers, and `<app> self status` reports the
installed version, the install channel, and pending steps (strictcli todo
`self-update-and-data-migration-steps.md`). Two parts of that design belong to
rlsbl, because rlsbl builds and publishes every artifact and gates every
release.

## Part 1: channel stamps on every CI-built artifact

`self update` must know how the binary was installed and must never guess it.
For everything rlsbl builds, the answer is written into the binary:

- At link time (`-ldflags "-X ..."`, passed per build through goreleaser), each
  artifact is stamped with its version, its commit, and its channel: GitHub
  release archive, Homebrew, npm package, or PyPI package.
- The npm and PyPI packages ship the release binary, so each channel needs its
  own stamped build: the same code, linked with a different channel value.
- A release check refuses any built artifact that lacks the stamp, so the
  stamp is enforced, not conventional.
- Out of rlsbl's reach, by design: `go install module@v0` builds on the user's
  machine from the module proxy. strictcli recognizes it from Go's embedded
  build info (a real module version and no stamp); development builds from a
  checkout are recognized and refused by strictcli. No rlsbl change is needed
  for those.

Open: the stamp's variable names and format, agreed with strictcli's API.

## Part 2: release rules for data migration steps

An app's stored data is stamped with the app version that last migrated it,
and steps are registered at the release version that introduced them. rlsbl
enforces semver on steps at release:

- a patch release that registers a step is refused (patches never change
  stored data);
- after graduation, a minor release may register only additive steps;
- a major release, or any pre-stable minor release, may register any step;
- a changelog entry marked breaking that changes a stored format, without a
  registered step converting it, is refused.

How rlsbl learns which steps a release registers is open: for example from the
app's `help --json` document, or a dedicated machine-readable listing strictcli
provides.

## Dependencies and order

Part 2 depends on strictcli's step-registration API; Part 1 depends only on
agreeing the stamp format with strictcli. strictcli releases its API first.

## Effort

Medium. Part 1: goreleaser configuration per channel, the stamp, and a release
check with red-green tests. Part 2: reading registered steps and four release
rules, each with a test fixture.
