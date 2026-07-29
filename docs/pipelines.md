---
description: "Pipeline architecture: 10 built-in types, 3 auth patterns, custom assets, capability gating, launcher shims with download modes, and migration guide."
---

# Pipelines

## Overview

Pipelines handle **publishing** — where and how a release is distributed. They are configured in `.rlsbl/config.json` under the `pipelines` key, which supports 10 built-in pipeline types across 3 authentication patterns (token, credential, and unauthenticated). Each pipeline entry has a user-chosen name and specifies its type, auth mechanism, and optional asset configuration.

Pipelines are distinct from targets: targets determine which files get version-bumped (auto-detected from manifests), while pipelines determine where the release artifact is published (explicitly configured). A project can have an npm target for versioning but a cloudflare-pages pipeline for publishing, or multiple pipelines publishing to different registries.

## Targets vs pipelines

| Concern | Targets | Pipelines |
| --- | --- | --- |
| Purpose | Version bumping | Publishing |
| Discovery | Auto-detected from manifests | Explicitly configured |
| Config location | Auto or `targets` array in config.json | `pipelines` object in config.json |
| Cardinality | One per ecosystem per project | Any number, user-named |
| Example | npm target bumps package.json version | npm pipeline runs `npm publish` in CI |

A project with no pipelines configured simply does not publish anywhere — version bumps, tags, and GitHub Releases still happen via targets.

## Configuration

Pipelines are configured in `.rlsbl/config.json` under the `pipelines` key. Each entry is keyed by a user-chosen name (any valid JSON string) and requires at minimum a `type` field (one of 10 built-in types) and a `local` boolean field indicating whether publishing happens on the developer machine or in CI:

```json
{
  "pipelines": {
    "my-pipeline-name": {
      "type": "npm",
      "local": false
    }
  }
}
```

### Fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `type` | string | Yes | One of the 9 built-in pipeline types (see table below) |
| `local` | bool | Yes | Whether to publish from the developer machine. `false` means CI handles it. |
| `artifact` | string | Yes (type `go`) | `binary` or `library`. Selects the go publish workflow. No default. See [go](#go). |
| `token_var` | string | No | Env var name for the publish token. Each type has a default. |
| `username_var` | string | No | Env var for username auth (docker only). |
| `password_var` | string | No | Env var for password auth (docker only). |
| `assets` | bool | No | Enable building and uploading target-specific artifacts to GitHub Releases. |
| `max_asset_size_mb` | int | When `assets` or `custom_assets` is set | Maximum artifact size in MB. Release fails if any artifact exceeds this. |
| `custom_assets` | array | No | List of custom build artifacts. Each entry: `{name, build}`. |

## Pipeline types

There are 10 built-in pipeline types covering all major package registries and deployment platforms. Each type implements ecosystem-specific authentication, build commands, and publish logic while sharing the common `BasePipeline` interface for custom assets and lifecycle hooks.

:-: table-pipelines

## Class hierarchy

All 10 pipeline implementations inherit from `BasePipeline`, which provides no-op defaults for publish and build steps plus the shared `build_custom_assets()` implementation. Two intermediate mixins add authentication patterns: `TokenPipeline` for single-token auth (5 pipelines) and `CredentialPipeline` for username/password pairs (1 pipeline).

| Class | Auth pattern | Pipelines |
| --- | --- | --- |
| `BasePipeline` | None (direct subclass) | go (proxy notification), maven (flexible auth), maven-central (Central Portal credentials), cloudflare-pages (selfdoc CLI) |
| `TokenPipeline(BasePipeline)` | Single env var token | npm, pypi, cargo, deno, hex |
| `CredentialPipeline(BasePipeline)` | Username + password env vars | docker |

`TokenPipeline` validates that the token env var is set before attempting publish and passes it to the ecosystem-specific publish command. `CredentialPipeline` validates both username and password env vars.

## Custom assets

Custom assets allow attaching arbitrary build artifacts to GitHub Releases alongside the source code archive. Each asset has a user-defined build command, an expected output filename, and a configurable maximum file size enforced via `max_asset_size_mb` (no default -- must be explicitly set when assets are enabled). The complete 7-step flow during `rlsbl release run`:

1. Config defines build commands and output filenames in `custom_assets`
2. Creates distribution directory: `.rlsbl/dist/<pipeline-name>/`
3. Runs each build command with `$RLSBL_DIST_DIR` env var pointing to the dist directory
4. Verifies each expected output file exists in `$RLSBL_DIST_DIR`
5. Validates file size against `max_asset_size_mb` (hard error if exceeded)
6. Uploads all artifacts to GitHub Release via `gh release upload <tag> --clobber`
7. Cleans up the dist directory

### Custom assets config example

```json
{
  "pipelines": {
    "release-bins": {
      "type": "go",
      "local": true,
      "assets": true,
      "max_asset_size_mb": 50,
      "custom_assets": [
        {
          "name": "mytool-linux-amd64",
          "build": "GOOS=linux GOARCH=amd64 go build -o $RLSBL_DIST_DIR/mytool-linux-amd64 ./cmd/mytool"
        },
        {
          "name": "mytool-darwin-arm64",
          "build": "GOOS=darwin GOARCH=arm64 go build -o $RLSBL_DIST_DIR/mytool-darwin-arm64 ./cmd/mytool"
        }
      ]
    }
  }
}
```

## Capability gating

Pipeline steps are gated on 2 target capabilities (`publish` and `build_assets`). Each release target declares which pipeline operations it supports, and rlsbl skips steps the target cannot handle rather than failing. This allows you to configure pipelines broadly without worrying about targets that lack publish or build support — the system gracefully omits inapplicable steps while still executing the rest of the release flow.

| Capability | Effect when absent |
| --- | --- |
| `publish` | The publish step is skipped entirely for that target |
| `build_assets` | Asset building is skipped for that target |

This means a target that does not support publishing (e.g., a documentation-only target) will not attempt to run any pipeline's publish step, even if pipelines are configured. The pipeline config remains valid — it simply has no effect for that target.

## Migration from old publish key

The old `publish` key in `.rlsbl/config.json` is no longer recognized. Running `rlsbl release run` with a `publish` key present produces a **hard error** — no fallback, no deprecation warning. The migration is mechanical: the new `pipelines` format is a strict superset of the old `publish` value, adding only a user-chosen name for each entry and an explicit `local` field. Most projects need fewer than 5 lines changed in their config.

To migrate:

1. Read the old `publish` value (it was a dict with `type` and optionally `local`)
2. Create a `pipelines` entry with a descriptive name
3. Copy `type` and `local` fields
4. Add `token_var` if you were using a non-default env var
5. Remove the old `publish` key

Before:
```json
{
  "publish": {
    "type": "npm",
    "local": false
  }
}
```

After:
```json
{
  "pipelines": {
    "npm-publish": {
      "type": "npm",
      "local": false
    }
  }
}
```

## Example configs

### npm publish via CI (most common)

```json
{
  "pipelines": {
    "npm": {
      "type": "npm",
      "local": false
    }
  }
}
```

CI workflow uses `NPM_TOKEN` secret. No local publish step runs.

### Local Cloudflare Pages deploy

```json
{
  "pipelines": {
    "docs": {
      "type": "cloudflare-pages",
      "local": true
    }
  }
}
```

Publishes from the developer machine using selfdoc's deploy integration. Reads `CF_PAGES_API_TOKEN` and `CF_ACCOUNT_ID` from the environment.

### Multiple pipelines

```json
{
  "pipelines": {
    "registry": {
      "type": "pypi",
      "local": false
    },
    "site": {
      "type": "cloudflare-pages",
      "local": true
    }
  }
}
```

PyPI publishing happens in CI; docs deploy happens locally in a post-release hook.

## Per-type reference

### npm

- **Class:** `TokenPipeline`
- **Default token env var:** `NPM_TOKEN`
- **Auth pattern:** Single token. CI workflow sets `//registry.npmjs.org/:_authToken` from the secret.
- **Publish command:** `npm publish --provenance --access public` (always uses npm CLI directly for local publish, regardless of which package manager the project uses).
- **CI template:** Detects which package manager the project uses (npm, pnpm, or yarn) and generates the appropriate install and publish steps for that package manager.
- **Quirks:** Package manager detection is based on lockfile presence (`package-lock.json` for npm, `pnpm-lock.yaml` for pnpm, `yarn.lock` for yarn). Priority order is pnpm > yarn > npm. Detection walks up directories until it finds a `.git` directory. The detection only affects CI template selection, not the local publish command.

### pypi

- **Class:** `TokenPipeline`
- **Default token env var:** `PYPI_TOKEN` (fallback: `TWINE_PASSWORD`)
- **Auth pattern:** Dual-token fallback. Checks `PYPI_TOKEN` first, then `TWINE_PASSWORD`. However, the preferred approach is OIDC Trusted Publishing, which requires no token at all — CI authenticates via GitHub's OIDC provider and `pypa/gh-action-pypi-publish`.
- **Publish command:** `uv build` followed by `uv publish` (passes token via `UV_PUBLISH_TOKEN` env var). No twine fallback.
- **CI template:** Uses `pypa/gh-action-pypi-publish` with `id-token: write` permission for OIDC.
- **Quirks:** For new packages, a pending publisher must be configured on pypi.org before the first release. No local `uv publish` or token needed when using Trusted Publishing. Overrides the base `TokenPipeline.publish()` method to implement dual-token resolution.

### go

- **Class:** `BasePipeline` (no token required)
- **Default token env var:** None
- **Auth pattern:** No authentication. Go modules are published by pushing a tagged commit — the Go module proxy picks it up automatically.
- **Publish command:** Notifies the Go module proxy (`proxy.golang.org`) by requesting the module at the new version, then runs `go install <path>` for every path declared in `install_paths`.
- **CI template:** Minimal — Go publish is just the tag push plus a proxy notification step.
- **Required `artifact` key:** Every `type: "go"` pipeline **must** declare `artifact`, either `"binary"` or `"library"`. There is no default. The value selects the publish workflow that gets scaffolded:
  - `"binary"` — a CLI/command whose GitHub Release assets are built by goreleaser (`publish.yml`).
  - `"library"` — an importable module verified against the Go module proxy (`publish-library.yml`); no goreleaser, no release assets.

  A wrong or missing value produces a broken workflow, so validation is a hard error rather than a silent guess. `rlsbl scaffold` sets the key automatically by auto-detecting the project layout (a project with no `package main` is a library, otherwise a binary), and the validation error message includes the same auto-detected suggestion — but the operator must commit the choice explicitly.
- **Library tag handling:** The library publish workflow bakes the module path from `go.mod` at scaffold time (correct even for monorepo subdirectory modules, whose proxy-visible tags are the companion subdir tag `<subdir>/vX.Y.Z`) and derives the version from the release tag, handling plain (`v1.2.3`), releasable (`<name>@v1.2.3`), and subdir (`<subdir>/v1.2.3`) tag formats.
- **Private modules:** A private Go module cannot be verified against the public proxy (`proxy.golang.org` refuses to serve private modules). Private Go libraries must set `publish_mode` `"none"` in `.rlsbl/config.json`, which suppresses the publish job entirely — no publish workflow is scaffolded.
- **Quirks:** Pipelines with `local: true` **must** declare `install_paths` (a list of main-package dirs relative to the project root, e.g. `["./cmd/mytool"]`). Missing or invalid declarations are hard errors; each declared path is validated against `go list` (it must be a `package main` dir). There is no auto-detection fallback — detection only validates declarations.

### cargo

- **Class:** `TokenPipeline`
- **Default token env var:** `CARGO_REGISTRY_TOKEN`
- **Auth pattern:** Single token. The token is passed to `cargo publish` via the `--token` flag or the `CARGO_REGISTRY_TOKEN` env var.
- **Publish command:** `cargo publish`
- **CI template:** Standard publish step with the token from GitHub secrets.
- **Quirks:** Standard single-token pattern with no special fallback logic.

### deno

- **Class:** `TokenPipeline`
- **Default token env var:** `DENO_TOKEN` (fallback: `JSR_TOKEN`)
- **Auth pattern:** Dual-token fallback, similar to pypi. Checks `DENO_TOKEN` first, then `JSR_TOKEN`.
- **Publish command:** `deno publish`
- **CI template:** Passes the token via environment variable to the publish step.
- **Quirks:** Publishes to JSR (JavaScript Registry). The dual-token fallback accommodates projects that use either env var name.

### hex

- **Class:** `TokenPipeline`
- **Default token env var:** `HEX_API_KEY`
- **Auth pattern:** Single token passed via `HEX_API_KEY` env var.
- **Publish command:** `mix hex.publish --yes`
- **CI template:** Standard publish step with the token from GitHub secrets.
- **Quirks:** Standard single-token pattern. The `--yes` flag is required to skip the interactive confirmation prompt.

### maven

- **Class:** `BasePipeline` (flexible auth)
- **Default token env var:** `GITHUB_TOKEN` (configurable via `token_var` in pipeline config).
- **Auth pattern:** Single token read from the configured `token_var` env var (defaults to `GITHUB_TOKEN`). Subclasses `BasePipeline` directly rather than `TokenPipeline` because it implements its own token resolution with a different default.
- **Publish command:** Detects gradle vs maven build system. Runs `./gradlew publish` for Gradle projects or `mvn deploy` for Maven projects.
- **CI template:** Generates appropriate publish steps based on detected build system and target registry.
- **Quirks:** Build system detection is based on the presence of a `gradlew` script (Gradle) or `pom.xml` (Maven) in the project directory. Errors if neither is found. Does not check for `build.gradle` or `build.gradle.kts` directly.

### maven-central

- **Class:** `BasePipeline` (own credential resolution)
- **Default credential env vars:** `ORG_GRADLE_PROJECT_mavenCentralUsername`, `ORG_GRADLE_PROJECT_mavenCentralPassword`, `ORG_GRADLE_PROJECT_signingInMemoryKey`, `ORG_GRADLE_PROJECT_signingInMemoryKeyPassword`
- **Auth pattern:** Four env vars for Central Portal user tokens and GPG signing. All four must be set for local publish. Optional: `ORG_GRADLE_PROJECT_signingInMemoryKeyId` (for specific GPG subkey selection).
- **Publish command:** Detects gradle vs maven build system. Runs `./gradlew publishAndReleaseToMavenCentral` for Gradle projects (delegates to the vanniktech/gradle-maven-publish-plugin) or `mvn deploy` for Maven projects with Central Portal configuration.
- **CI template:** Uses `publish-central.yml.tpl` (separate from the maven pipeline's `publish.yml.tpl`). Passes credentials as GitHub secrets.
- **Quirks:** Subclasses `BasePipeline` directly (not `TokenPipeline` or `CredentialPipeline`) because it requires four env vars rather than the standard one or two. Build system detection is identical to the `maven` pipeline (presence of `gradlew` or `pom.xml`). A `maven-central-metadata` quality check validates POM metadata (name, description, url, licenses, developers, scm), sources/javadoc jar generation, and signing configuration when a `maven-central` pipeline is configured.

### docker

- **Class:** `CredentialPipeline`
- **Default credential env vars:** `DOCKER_USERNAME` + `DOCKER_PASSWORD`
- **Auth pattern:** Username and password pair. Both must be set. Configured via `username_var` and `password_var` in the pipeline config.
- **Publish command:** `docker build` with `--build-arg VERSION=<version>`, then `docker push` with the versioned tag, then `docker tag` to create a `latest` tag, then pushes `latest`. No explicit `docker login` step in local publish (credentials are validated but login is assumed to be pre-configured).
- **CI template:** Login step followed by build and push steps.
- **Quirks:** Requires `image` and `registry` fields in the pipeline config to construct the full image reference (`<registry>/<image>:<version>`). Both the versioned and `latest` tags are pushed.

### cloudflare-pages

- **Class:** `BasePipeline`
- **Default token env var:** None (uses `CF_PAGES_API_TOKEN` and `CF_ACCOUNT_ID` env vars for local deploys).
- **Auth pattern:** Requires `CF_ACCOUNT_ID` and `CF_PAGES_API_TOKEN` from the environment when publishing locally. These are reported by `required_env_vars()`.
- **Publish command:** `selfdoc deploy` (requires `selfdoc` on PATH). No Wrangler fallback.
- **CI template:** Minimal — most Cloudflare Pages projects deploy locally from post-release hooks rather than CI.
- **Quirks:** The simplest pipeline implementation. Primarily used for documentation sites that deploy alongside library releases. Requires `selfdoc` tool on PATH; errors if not found. 300-second timeout on the deploy command.

## Launcher artifact kind

The `artifact: "launcher"` pipeline kind produces a wrapper package that downloads a pre-built binary from a GitHub Release. This is for projects that have a Go (or other compiled) binary and want to distribute it via npm and/or PyPI as a convenience shim.

### Config shape

```json
{
  "pipelines": {
    "go":   {"type": "go",   "local": false, "target": "go",   "artifact": "binary"},
    "npm":  {"type": "npm",  "local": false, "target": "npm",  "artifact": "launcher",
             "wraps": "go", "binary_source": "github-release", "download": "postinstall",
             "provenance": true},
    "pypi": {"type": "pypi", "local": false, "target": "pypi", "artifact": "launcher",
             "wraps": "go", "binary_source": "github-release", "download": "first-run"}
  }
}
```

### Required keys

| Key | Type | Description |
| --- | --- | --- |
| `artifact` | `"launcher"` | Selects the launcher publish template instead of the standard publish template |
| `wraps` | string | Name of the pipeline that produces the binary. Must reference a pipeline with `artifact: "binary"`. |
| `binary_source` | `"github-release"` | Where the launcher downloads binaries from. Only `"github-release"` is supported. |
| `download` | `"first-run"` \| `"postinstall"` | **When** the binary is fetched. `"postinstall"` (npm only) downloads it at `npm install` time; `"first-run"` downloads it lazily on the first CLI invocation (zero network I/O at install). No default. |

All four keys are mandatory when `artifact` is `"launcher"`. Missing or invalid values are hard errors at config validation and scaffold time. `download: "postinstall"` is an npm-only mechanism -- a non-npm launcher (e.g. PyPI, which has no install-time hook) with `download: "postinstall"` is a hard error and must use `"first-run"`.

### `download` mode semantics

The `download` key selects **when** the wrapped binary is fetched from GitHub Releases. This is a deployment-shape decision with no default -- the operator must explicitly choose between fetching at install time or lazily on first invocation. Each mode has different trade-offs for network behavior, install speed, and offline usability that affect how end users experience the tool:

- **`postinstall` (npm only):** The wrapper ships a `postinstall` script (`scripts/postinstall.cjs`) that runs at `npm install` time. It maps `process.platform`/`process.arch` to goreleaser's OS/arch naming, downloads the matching release asset **and** the release's `checksums.txt`, SHA-256-verifies the asset against the matching `checksums.txt` line **before** installing it into the package's `vendor/` directory, and hard-fails on a checksum mismatch or a 404. A `bin/launcher.cjs` stub then execs the vendored binary, passing argv through. Node stdlib only -- zero runtime dependencies.
- **`first-run` (npm and PyPI):** `npm install` performs **zero network I/O** -- no `postinstall` script is emitted. The wrapper ships a single self-contained `bin/launcher.cjs` (npm) or console-script module (PyPI) that, on the **first CLI invocation**, resolves the exact package version, downloads the matching release asset **and** `checksums.txt`, SHA-256-verifies before caching, extracts the binary to a platform-specific cache directory (`~/.cache/<tool>/` on Linux, `~/Library/Caches/<tool>/` on macOS, `%LOCALAPPDATA%\<tool>\` on Windows), then execs it -- passing argv through. Subsequent invocations exec the cached binary directly (no network). This is the required mode for consumers whose package must not touch the network at install time (e.g. library-only installs). Stdlib only -- zero runtime dependencies.

PyPI has no `postinstall` hook, so PyPI launchers always use `first-run`.

Embedded platform wheels (building the binary into the wheel for each platform) are a different distribution model -- that is the per-platform binary-wrapper family, not the launcher. Launchers are download-at-install/run shims.

### Manifest is the name authority

Scaffold never invents or writes the package name field in the launcher target's manifest (`package.json` for npm, `pyproject.toml` for PyPI). The manifest at the launcher target's declared path is the name authority. If the manifest is absent, scaffold hard-errors and directs the user to create it with a `rlsbl check-name`'d name.

Around that pre-existing manifest, scaffold generates the shim code and fills **only the missing non-name fields, exactly once** -- never touching the name or any value the user already set, so a second scaffold is a byte-level no-op:

- **npm (`download: "postinstall"`):** `bin` (maps the command name to `bin/launcher.cjs`), `scripts.postinstall` (`node scripts/postinstall.cjs`), and `files` (`["bin", "scripts", "vendor"]`, so the shims ship in the tarball).
- **npm (`download: "first-run"`):** `bin` and `files` (`["bin"]`) only. **No** `scripts.postinstall` -- installing the package performs zero network I/O.
- **PyPI:** the `[project.scripts]` console-script entry (mapping the command name to the launcher module's `main`).

The `wrapper-producer` check additionally hard-errors if one of these required fields is later deleted from the manifest, naming the field -- a deletion would silently break the published wrapper. The required-field set is `download`-mode-aware: in `first-run` mode `scripts.postinstall` is not required (and not expected), only `bin` and `files`.

### Hard constraint: goreleaser default asset naming

Launchers depend on goreleaser's **default** asset naming and the literal `checksums.txt` filename. The producer's `.goreleaser.yml` must emit assets named `<ProjectName>_<Version>_<Os>_<Arch>.<ext>` (tar.gz, or zip on Windows) and a checksum file named exactly `checksums.txt`. The scaffolded config does this out of the box.

Both the CI verify step (which probes a representative asset URL and the `checksums.txt` URL for HTTP 404) and the install/first-run shims (which reconstruct these names to download and SHA-256-verify) are built on this contract. A custom `name_template` in `.goreleaser.yml` breaks it and is unsupported: the verify step turns the drift into a red publish job at the release that introduced it, rather than letting silent 404s reach every future install.

### Verification closures

Two structural closures work together to prevent broken wrapper packages from reaching registries. The first closure enforces ordering so the binary exists before the wrapper publishes, and the second closure verifies that the expected download URLs actually resolve. Both are enforced automatically in the generated CI workflows and cannot be bypassed:

1. **`needs` dependency chain.** Every launcher publish job emits `needs: [gate, <producer-job-key>]` in the generated CI workflow. This ensures the binary producer's publish job (e.g., goreleaser) has finished and uploaded its assets before the launcher attempts to publish. The merged publish generator and the monorepo router both preserve this dependency. Without this, a shim could publish before its binary exists -- a permanently broken package on a registry that cannot un-publish.

2. **URL verify-before-publish.** Before running `npm publish` or `uv publish`, the launcher workflow curls the constructed release-asset URL for a representative platform (linux/amd64) and hard-fails on HTTP 404. This catches goreleaser asset-naming drift (e.g., a custom `name_template` in `.goreleaser.yml`) at the release that introduced it, turning it into a red CI job instead of silent 404s for all future installs.

### `wrapper-producer` check

The `wrapper-producer` check (registered in the check system under the `project` and `preflight` tags) validates that every launcher pipeline's `wraps` field references an existing pipeline whose `artifact` is `"binary"`. This runs during `rlsbl check` and as part of the release preflight, catching misconfigurations before they reach CI.

### Decision rule: launcher vs monorepo members

- **One-off wrapper** (single Go binary distributed via npm or PyPI): use a subdirectory launcher target. The wrapper's `package.json` or `pyproject.toml` lives in a subdirectory (e.g., `packaging/npm/`), declared as an explicit target with a path.
- **Complex multi-artifact** (multiple packages that need coordinated versioning): use monorepo members in a shared releasable. Each member gets its own version bump, changelog, and independent publish pipeline. Multi-artifact releasables publish every member at the shared version.

Same-registry multiplicity is not a goal for launchers -- one launcher per registry per project.
