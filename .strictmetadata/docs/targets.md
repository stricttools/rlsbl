+++
description = "The release targets go, npm, and pypi: how a member's targets are declared or detected, the files each writes the version into, Go companion tags, built-in tests and local installs, and the support matrix every per-target fact is rendered from."
+++

# Release targets

A release target is the ecosystem a member's version lives in: it decides which files a release writes the version into, which tags the release owes besides the releasable's own, how the built-in tests run, and how `rlsbl dev install` installs the member. Publishing is a separate concern, declared as [pipelines](pipelines.md). rlsbl supports the targets in this table, and no others:

:-: table-targets

Every release creates the releasable's tag and its GitHub Release, whatever its targets.

## Declared or detected

A member's targets are the `targets` it declares in `.strictmetadata/releasables/releasables.toml`, each `{ name, path }` with `path` relative to the member, when it declares any. A member that declares none has the targets its manifests are detected as, in its own directory:

| Target | Detected from |
| --- | --- |
| go | a `go.mod` |
| npm | a `package.json` |
| pypi | a `pyproject.toml` with a `[project]` table; one holding only tool settings, or a virtual uv workspace root, is no pypi project |

A target name other than go, npm, and pypi is refused, naming the supported targets, and so is a declared target whose directory does not exist. `rlsbl targets` lists the supported targets against the member the working directory selects, saying whether its targets are declared or detected. `rlsbl scaffold --target <name>` declares a target the member lacks.

## Version files

A release writes its version into every version file of every target of the releasable's members, keeping every other byte of each file; a version that cannot be written there is refused before anything changes. Versions are `MAJOR.MINOR.PATCH` without a prefix or a pre-release suffix, in every target.

- **go**: the `VERSION` file beside `go.mod`. `go.mod` has no version field; the module's version is its tag.
- **npm**: the one top-level `version` of `package.json`. A key declared twice at the top level, a version that is not a string, and a missing version are refused.
- **pypi**: `[project].version` of `pyproject.toml`, and the package's `__version__`, the first top-level `__version__ =` line assigned one quoted string in the package's `__init__.py`. A computed or imported `__version__` is left alone; the `dunder-version-missing` check reports a version constant kept under any other name.

In a workspace, a releasable's version is also its version file, `.strictmetadata/releases/<releasable>/version`, which a releasable that publishes nothing has alone.

## Companion tags

A Go module is published by its tag, and the module proxy resolves a module below the repository root only by a tag carrying its path. A release therefore owes every Go member of the releasable a companion tag beside the releasable's own: `<path>/v<version>` below the root, and `v<version>` for a module at the root, unless that is the releasable's own tag. The release creates them, `release undo` deletes them, `release reconcile` repairs them, and the `go-companion-tags` check reports a missing one for the latest release.

## Per-target notes

### go

- A go `binary` pipeline gets `.goreleaser.yml` and a `version.go` from scaffold, and its `-X` linker flags are checked against the Go source by `ldflags-symbol`: a flag naming no injectable string variable links silently and leaves the binary reporting its fallback version.
- `go.mod` is parsed strictly; one the go command refuses is an error naming it. The checks `go-module-identity`, `go-module-major-suffix`, and `go-toolchain-declared` hold every module's path and toolchain line, and `go-workspace-require-current` and `go-workspace-replace` its relations to the workspace's other modules.
- A release refuses a module whose `go mod tidy` would change it, since the release's own tidy would commit dependency edits nobody made.
- The built-in tests run `go test ./... -race -short -count=1` in the module's directory, or the member's `test.go_command`.
- `rlsbl dev install --target global` runs `go install` for each main package the member's go pipeline names in `install_paths`; a module with main packages and no `install_paths` is refused.

### npm

- The package manager is read from the lockfile (`pnpm-lock.yaml`, `yarn.lock`, or `package-lock.json`), and the CI workflow is rendered for that package manager.
- `npm-private-mismatch` refuses a `package.json` with `"private": true` in a releasable that publishes from CI.
- The built-in tests run `npm test`. `rlsbl dev install` runs `npm link` globally and `npm install` into the project.

### pypi

- uv is required: the built-in tests run `uv run python -P -m pytest` after a sync, and there is no fallback to a bare pytest.
- The build rewrites a workspace member's path dependencies on its siblings into registry constraints for the published artifact, keeping each dependency's extras and environment marker.
- `rlsbl dev install` runs `uv tool install -e .` globally and `uv sync --all-packages` into the project.

## The support matrix

Every per-target fact rlsbl acts on is declared once, in the target's facts in `internal/targets`, and `internal/targets/gen` renders them into the committed `internal/targets/support-matrix.json`. The tables on this page are rendered from that file, and the `target-matrix-fresh` check keeps it equal to a fresh rendering. These are the questions every target answers:

:-: table-target-axes
