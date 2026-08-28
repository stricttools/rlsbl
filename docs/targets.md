---
description: "The rlsbl release targets including npm, PyPI, Go, Docker and Flutter, with auto-detection, the ReleaseTarget protocol, and per-axis support properties."
---

# Release targets

Each release target handles version reading, writing, and tag formatting for a specific ecosystem. Targets do not handle publishing — that is the responsibility of [pipelines](pipelines.md). The table below is the enumeration:

:-: table-targets

All targets share core release functionality: version bumping, git tagging, and GitHub Release creation. The table above shows the optional operations that vary by ecosystem, each derived from the target rather than declared beside it.

## Target vs Pipeline

Targets and pipelines serve orthogonal purposes in the release flow. Targets handle versioning (reading and writing version strings in manifest files), while pipelines handle publishing (uploading artifacts to registries). This separation allows flexible combinations where the versioning ecosystem differs from the publish destination:

| Concern | Targets | Pipelines |
| ------- | ------- | --------- |
| What they do | Read/write versions in manifest files | Publish artifacts to registries |
| Configured in | Auto-detected or `targets` array in config.json | `pipelines` dict in config.json |
| When they run | Version bump step of `rlsbl release run` | Publish step (CI or local) |
| Example | `pypi` target writes to `pyproject.toml` | `pypi` pipeline publishes via OIDC |

A project can have a target for versioning without a corresponding pipeline (e.g., Go libraries that need no publish step), or a pipeline type that differs from the target (e.g., an npm target with a `cloudflare-pages` pipeline for deployment).

## Auto-detection

When `rlsbl release run`, `rlsbl scaffold`, or `rlsbl targets` needs to know which targets apply, it calls `detect_targets(dir_path)` which scans the project directory for manifest files and applies content-based disambiguation when multiple targets could match. The detection logic follows two paths:

1. **Explicit configuration** — If `.rlsbl/config.json` contains a `targets` array, that list is authoritative. Each entry is either a string (`"npm"`) or a dict with `name` and optional `path` (for subdirectory targets). Unknown target names are warned and skipped.

2. **Auto-detection fallback** — If no `targets` array exists in config, every registered target's `detect()` method is called against the directory. Targets that return `True` are included.

The `auto_detectable` ClassVar on each target controls detection behavior. Which targets hold which value is the `Auto-detectable` column of the table above, not a list repeated here:

| Value | Meaning |
| ----- | ------- |
| `"yes"` | Standard file-based detection |
| `"conditional"` | Detects only when specific conditions are met beyond file presence (`plain` requires a `VERSION` file AND no other manifest) |
| `"no"` | Never auto-detected; must be declared in config |

### Detection priority

When multiple targets could match the same manifest file (e.g., a project with both `pubspec.yaml` and a `flutter:` section, or `build.gradle` matching both native-android and maven), targets use content-based checks to disambiguate and ensure exactly one target claims each project:

- **dart** excludes projects where `pubspec.yaml` contains a `flutter:` key
- **flutter** requires `pubspec.yaml` with a `flutter:` key
- **plain** yields to any other target's manifest file
- **native-android** vs **maven**: both use `build.gradle.kts`/`build.gradle`, but native-android checks for `com.android.application` plugin declaration

## Detection files

Each target class declares a `detection_files` ClassVar listing the filenames whose presence triggers detection; the `Detection files` column of the table above is that declaration, rendered. These filenames are aggregated into the `PROJECT_MANIFESTS` set used by workspace-level checks to detect unregistered projects in a monorepo. A target whose column is blank either decides by file *content* (native-android shares the Gradle files with maven and inspects them; native-ios scans for an `.xcodeproj`) or never auto-detects at all (swift-apple is selected only by explicit declaration). Shared manifests are disambiguated by content and annotated in the rendered column — flutter and dart share `pubspec.yaml`, and `plain` yields to every other target's manifest.

## The ReleaseTarget protocol

Every target implements a runtime-checkable Protocol that defines the interface for version management, detection, tag formatting, and CI template generation. Each target provides concrete implementations for its ecosystem's conventions. The key methods:

| Method | Purpose |
| ------ | ------- |
| `detect(dir_path) -> bool` | Check if this target applies to a directory |
| `read_version(dir_path) -> str` | Read the current version string from the manifest |
| `write_version(dir_path, version, ctx) -> list[str]` | Write a new version; returns list of modified file paths (relative to dir_path) |
| `version_file(dir_path) -> str or None` | Filename that holds the version (e.g., `"package.json"`) |
| `read_name(dir_path, ctx) -> str or None` | Read the project/package name from the manifest |
| `read_metadata(dir_path) -> dict` | Read optional metadata (license, description) |
| `tag_format(version) -> str` | Format the git tag (default: `v{version}`) |
| `monorepo_tag_format(name, version, path) -> str` | Format monorepo git tag (default: `{name}@v{version}`) |
| `monorepo_tag_glob(name, path) -> str` | Glob pattern matching all monorepo version tags |
| `template_vars(dir_path, ctx) -> dict` | Extract template variables for CI generation |
| `template_mappings(ctx) -> list[dict]` | Target-specific template-to-output-path mappings |
| `dev_install_command(project_dir) -> dict` | Return install specs for `rlsbl dev install` |
| `build(dir_path, version) -> None` | Pre-publish build step (no-op by default) |

:-: ref path="rlsbl.targets.protocol"

## BaseTarget defaults

All concrete targets extend `BaseTarget`, which provides sensible defaults for common operations so that individual targets only need to override ecosystem-specific behavior. The base class handles tag formatting, shared template mappings, and stub implementations for optional methods:

- Tag format: `v{version}` (standalone) / `{name}@v{version}` (monorepo)
- Shared template mappings: CHANGELOG.md, .gitignore, hooks, lint configs, unreleased.jsonl
- No-op stubs for `build()`, `dev_install_command()`, `read_name()`, `read_metadata()`
- `check_project_exists()` delegates to `detect()`

Individual targets override only the methods specific to their ecosystem.

:-: ref path="rlsbl.targets.base"

## What a target supports

There is no declared capability set. What a target supports is derived from the
target itself, one property per axis, so the answer cannot disagree with the
code that implements it. The axes are declared once, in
`rlsbl.targets.introspect`, and every registered target must answer every one
of them — a target that cannot, or an axis added to the protocol without a
declaration here, is an error at import time.

Completeness is stated by exclusion, so no naming convention can hide a fact:
every public attribute of `BaseTarget` must be either the source of an axis or
listed in `NON_AXIS_ATTRIBUTES` with the one line saying why it is not a
per-target fact (almost all of them are operations — `build`, `read_name`,
`yank` — whose "can this target do it at all?" is itself an axis). An
unclassified attribute, and an exclusion naming an attribute that no longer
exists, are both errors at import time:

:-: table-target-axes

Every target's answer to every axis is generated into
`rlsbl/data/support-matrix.json`, which is committed, rendered into the tables
on this page, and kept in step with the code by the `target-matrix-fresh`
check.

Each axis is consulted at the point of use. `rlsbl dev install` asks each target for
its install specs and skips the ones that have none, naming them. Four sites
decide whether to run a publication probe, and every one of them reads
`supports_publication_probe` rather than defaulting the answer:

| Site | What it decides |
| ---- | --------------- |
| Each pipeline's pre-publish check | whether to skip a version the registry already serves |
| The release's post-publish verification | which targets belong in the verified set |
| The undo evidence layer (`rlsbl release undo --version`) | whether a target can contribute registry evidence |
| `rlsbl release yank` | each target's publication status before removal |

The `name-consistency` check is not one of these: it asks every detected target
for a name and compares the ones that answer, reporting the targets that returned
nothing alongside the result rather than skipping them silently.

## Ecosystem classification

Each target has an `ecosystem` string used for display and grouping in commands like `rlsbl targets` and `rlsbl monorepo list`. Every target maps to a distinct ecosystem label, providing a human-readable name for each registry and platform; the `Ecosystem` column of the table above is the mapping.

## Per-target notes

### npm

- Reads/writes `package.json`
- Detects package manager by walking up to git root looking for lock files: `pnpm-lock.yaml` (pnpm), `yarn.lock` (yarn), `package-lock.json` (npm), falls back to npm
- Package manager choice affects CI template selection (separate templates for pnpm and yarn)
- Extracts `binCommand`, `repoName`, `registryUrl`, `publishSetup` template variables
- `dev_install`: global via `npm link`, local via `npm install`

### pypi

- Reads/writes `pyproject.toml` (via tomlkit for comment preservation)
- Also bumps `__version__` in `{pkg_name}/__init__.py` or `src/{pkg_name}/__init__.py` if present
- Build step handles monorepo path dependency rewriting (copies to temp dir, rewrites pyproject.toml, builds there)
- `dev_install`: global via `uv tool install -e .`, local via `uv sync`

### go

- Detection: `go.mod` presence
- Version stored in `VERSION` file (not go.mod — Go modules have no version field in go.mod)
- Monorepo tag format uses path prefix: `{path}/v{version}` (Go module proxy convention)
- Detects library vs binary projects via `go list` (any `package main` package, regardless of file names or layout)
- GoReleaser integration for binary projects; library projects need no publish step. Ambiguous multi-main layouts require `install_paths` on the go pipeline config.
- npm binary wrapper support, activated with `{"npm_wrapper": {"enabled": true}}` in `.rlsbl/config.json`. Per-platform packages publish under bare suffixed names (`<bin>-linux-x64`, `<bin>-darwin-arm64`, ...) plus a meta wrapper named `<bin>`. Scoped npm names (`@scope/name`) are banned by ecosystem policy; each bare per-platform name must be independently approved (`rlsbl check-name`) like any other package name. A stale `npm_wrapper.scope`/`npm_scope` key is a hard error.
- Homebrew tap support via `homebrew` config
- `dev_install`: `go install <install_paths>` from the go pipeline config (no venv concept); undeclared `install_paths` is a hard error

### deno

- Handles both `deno.json` and `deno.jsonc` (prefers `.json` when both exist)
- For `.jsonc` files, uses regex-based version replacement to preserve comments
- For `.json` files, uses standard JSON rewrite preserving indent
- `version_file()` resolves dynamically based on which config file exists


### dart

- Reads/writes `pubspec.yaml` using ruamel.yaml for comment preservation
- Strips build number suffix (`+N`) when reading, handles it when writing
- Build number strategy configurable via `build_number.enabled` and `build_number.strategy` in config
- Excludes projects with `flutter:` key (those belong to the flutter target)

### flutter

- Extends `DartTarget` (inheritance, not duplication)
- Detection: `pubspec.yaml` must contain a `flutter:` key
- Inherits all dart version read/write logic including build number handling
- No detection_files of its own (shares pubspec.yaml with dart)

### swift

- Detection: `Package.swift` presence
- Version stored in `VERSION` file
- `dev_install`: global via `swift build`, no venv concept

### swift-apple

- Extends `SwiftTarget` (inheritance)
- Never auto-detected (`auto_detectable = "no"`) — must be declared in `.rlsbl/config.json` targets array
- Provides macOS-only CI templates (uses `macos-latest` runners instead of `ubuntu-latest`)
- No `dev_install` support

### zig

- Detection: `build.zig.zon` or `build.zig`
- Version stored in `VERSION` file with automatic `build.zig.zon` synchronization
- npm binary wrapper support for cross-compiled binaries, activated with `{"npm_wrapper": {"enabled": true}}`. Publishes bare per-platform names (`<bin>-linux-x64`, ...) plus a meta wrapper named `<bin>`; scoped names are banned and each name needs explicit `rlsbl check-name` approval. A stale `npm_wrapper.scope`/`npm_scope` key is a hard error.
- Cross-compilation target map for 6 platforms (linux/darwin/win32, x64/arm64)

### docker

- Detection: `Dockerfile` presence
- Version stored in `VERSION` file
- Image name derived from config (`docker.image`) or directory name

### maven

- Detection: `build.gradle.kts`, `build.gradle`, or `pom.xml`
- Supports three build systems: Maven (pom.xml), Gradle (build.gradle), Gradle Kotlin DSL (build.gradle.kts)

### spec

- Detection: `version.json` file presence
- Version: reads/writes `{"version": "X.Y.Z"}` in `version.json`
- Its CI template is a stub, for users to add their own validation commands; there is no publish step
- Use case: spec-only projects that need version tracking without any build or publish step — the tagged GitHub Release is the publication

### pgdesign

- Detection: `pgdesign.toml` file presence
- Version: reads/writes the `version` field in `pgdesign.toml`
- No publish mechanism — version bumping only (the tagged GitHub Release is the artifact)
- Use case: PostgreSQL schema design projects managed by the pgdesign tool

### native-ios

- Content-based detection: scans for `.xcodeproj/project.pbxproj` with MARKETING_VERSION
- Also supports Tuist `Project.swift`
- See [native-targets.md](native-targets.md) for details

### native-android

- Content-based detection: checks `build.gradle`/`build.gradle.kts` for `com.android.application` plugin
- Manages both `versionName` (semver) and `versionCode` (integer, auto-incremented)
- See [native-targets.md](native-targets.md) for details

### plain

- Detection: conditional — `VERSION` file must exist AND no other target manifest is present
- Version: reads/writes plain text `VERSION` file (single line, e.g. `0.5.2`)
- Supports nothing beyond version bumping and tagging — its row in the table above is blank on every optional axis, so scaffold generates no CI workflow, `rlsbl dev install` has nothing to run, and no pipeline links to it
- The stand-off set: plain will not auto-detect when any other target's manifest is present. That set is derived from every registered target's `detection_files`, plus `Cargo.toml` and `selfdoc.json` — manifests left behind by retired targets that no current target claims
- Use case: projects that need version tracking but don't fit any ecosystem (e.g., documentation-only repos, script collections, infrastructure projects)
- Also bumps `pyproject.toml` version if that file exists with a `[project].version` field

## Check support matrix

Some checks are universal (they run for any target), while others only apply to targets with language-specific import scanners or AST analysis. This matrix shows which target-specific checks support which targets.

:-: table-feature-matrix

All checks not listed here are universal and run for every target.

## Target implementations

The base target class defines the shared interface for version reading, version writing, detection, and version file location. Every concrete target implementation inherits from this base and override the methods relevant to their ecosystem's versioning conventions.

:-: ref path="rlsbl.targets.base"
