---
description: "All 18 rlsbl release targets covering npm, PyPI, Go, Cargo, Deno, Hex, Maven, Swift, Zig, Docker, Dart, Flutter, and more — with auto-detection, the ReleaseTarget protocol, capabilities, and ecosystem classification."
---

# Release targets

rlsbl supports 18 release targets. Each target handles version reading, writing, and tag formatting for a specific ecosystem. Targets do not handle publishing — that is the responsibility of [pipelines](pipelines.md).

:-: table-targets

All targets share core release functionality: version bumping, git tagging, and GitHub Release creation. The table above shows optional capabilities that vary by ecosystem.

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

The `auto_detectable` ClassVar on each target controls detection behavior:

| Value | Meaning | Example targets |
| ----- | ------- | --------------- |
| `"yes"` | Standard file-based detection | npm, pypi, go, cargo, deno, hex, maven, docker, dart, zig, spec, pgdesign, swift, native-ios, native-android |
| `"conditional"` | Detects only when specific conditions are met beyond file presence | plain (VERSION exists AND no other manifest present) |
| `"no"` | Never auto-detected; must be declared in config | swift-apple |

### Detection priority

When multiple targets could match the same manifest file (e.g., a project with both `pubspec.yaml` and a `flutter:` section, or `build.gradle` matching both native-android and maven), targets use content-based checks to disambiguate and ensure exactly one target claims each project:

- **dart** excludes projects where `pubspec.yaml` contains a `flutter:` key
- **flutter** requires `pubspec.yaml` with a `flutter:` key
- **plain** yields to any other target's manifest file
- **native-android** vs **maven**: both use `build.gradle.kts`/`build.gradle`, but native-android checks for `com.android.application` plugin declaration

## Detection files

Each of the 18 target classes declares a `detection_files` ClassVar listing the filenames whose presence triggers detection. These filenames are aggregated into the `PROJECT_MANIFESTS` set used by workspace-level checks to detect unregistered projects in a monorepo. The table below shows each target's detection files:

| Target | Detection files |
| ------ | --------------- |
| npm | `package.json` |
| pypi | `pyproject.toml` |
| go | `go.mod` |
| cargo | `Cargo.toml` |
| deno | `deno.json`, `deno.jsonc` |
| hex | `mix.exs` |
| maven | `build.gradle.kts`, `build.gradle`, `pom.xml` |
| swift | `Package.swift` |
| swift-apple | (none -- opt-in only) |
| dart | `pubspec.yaml` |
| flutter | (none -- content-based, shares `pubspec.yaml` with dart) |
| docker | `Dockerfile` |
| zig | `build.zig.zon`, `build.zig` |
| spec | `version.json` |
| pgdesign | `pgdesign.toml` |
| native-ios | (none -- content-based, scans for `.xcodeproj`) |
| native-android | (none -- content-based, shares gradle files with maven) |
| plain | (none -- conditional on `VERSION` file with no other manifests) |

## The ReleaseTarget protocol

All 18 targets implement a runtime-checkable Protocol that defines the interface for version management, detection, tag formatting, and CI template generation. Each target provides concrete implementations for its ecosystem's conventions. The key methods:

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

## Capabilities

Each target declares a `capabilities` frozenset containing zero or more capability strings that gate behavior in the release flow and check system. Commands and checks query these capabilities at runtime to determine which operations are valid for a given target:

| Capability | Meaning |
| ---------- | ------- |
| `read_name` | Target can extract the package name from its manifest |
| `read_metadata` | Target can extract license and description |
| `ci_templates` | Target provides CI workflow templates for scaffold |
| `dev_install` | Target supports `rlsbl dev install` (editable local installs) |

Capabilities are checked at runtime. For example, `rlsbl dev install` skips targets without `dev_install`, and the `name-consistency` check skips targets without `read_name`.

## Ecosystem classification

Each target has an `ecosystem` string used for display and grouping in commands like `rlsbl targets` and `rlsbl monorepo list`. The 18 targets map to 18 distinct ecosystem labels, providing human-readable names for each registry and platform:

| Ecosystem | Targets |
| --------- | ------- |
| Node.js / npm | npm |
| Python / PyPI | pypi |
| Go modules | go |
| Rust / crates.io | cargo |
| Deno / JSR | deno |
| Elixir / Hex | hex |
| Java / Maven | maven |
| Swift (SPM) | swift |
| Swift (Apple) | swift-apple |
| Dart / pub.dev | dart |
| Flutter | flutter |
| Docker | docker |
| Zig | zig |
| Specification | spec |
| PostgreSQL | pgdesign |
| iOS | native-ios |
| Android | native-android |
| Plain | plain |

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
- Detects library vs binary projects (checks for `package main` in root files or `cmd/` layout)
- GoReleaser integration for binary projects; library projects need no publish step
- npm binary wrapper support via `npm_wrapper` config
- Homebrew tap support via `homebrew` config
- `dev_install`: `go install ./...` (no venv concept)

### deno

- Handles both `deno.json` and `deno.jsonc` (prefers `.json` when both exist)
- For `.jsonc` files, uses regex-based version replacement to preserve comments
- For `.json` files, uses standard JSON rewrite preserving indent
- `version_file()` resolves dynamically based on which config file exists

### cargo

- Reads/writes `Cargo.toml` using tomlkit for round-trip editing
- `dev_install`: global via `cargo install --path .`, local via `cargo build`

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
- npm binary wrapper support for cross-compiled binaries
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
- Capabilities: `read_name` and `ci_templates` (a stub CI template for users to add their own validation commands) — no `dev_install`, no publish
- Use case: spec-only projects that need version tracking without any build or publish step — the tagged GitHub Release is the publication

### pgdesign

- Detection: `pgdesign.toml` file presence
- Version: reads/writes the `version` field in `pgdesign.toml`
- Capabilities: minimal — no `ci_templates`, no `dev_install`
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
- Capabilities: none (zero capabilities — no `read_name`, no `read_metadata`, no `ci_templates`, no `dev_install`)
- Consequences of zero capabilities: no CI templates generated by scaffold, no `rlsbl dev install` support, no publish pipeline, no `build_assets`
- The exclusion list: plain will not auto-detect if any of the 17 other target manifests exist (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `deno.json`, `deno.jsonc`, `mix.exs`, `build.gradle.kts`, `build.gradle`, `pom.xml`, `Package.swift`, `pubspec.yaml`, `Dockerfile`, `build.zig.zon`, `build.zig`, `version.json`, `pgdesign.toml`)
- Use case: projects that need version tracking but don't fit any ecosystem (e.g., documentation-only repos, script collections, infrastructure projects)
- Also bumps `pyproject.toml` version if that file exists with a `[project].version` field

## Check support matrix

Some checks are universal (they run for any target), while others only apply to targets with language-specific import scanners or AST analysis. This matrix shows which target-specific checks support which targets.

:-: table-feature-matrix

All checks not listed here are universal and run for every target.

## Target implementations

The base target class defines the shared interface for version reading, version writing, detection, and version file location. All 18 concrete target implementations inherit from this base and override the methods relevant to their ecosystem's versioning conventions.

:-: ref path="rlsbl.targets.base"
