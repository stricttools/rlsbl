---
description: "Pipeline architecture for publish orchestration — types, auth patterns, custom assets, capability gating, and migration from the old publish key."
---

# Pipelines

## Overview

Pipelines handle **publishing** — where and how a release is distributed. They are configured in `.rlsbl/config.json` under the `pipelines` key. Each pipeline entry has a user-chosen name and specifies its type, auth mechanism, and optional asset configuration.

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

Each entry in the `pipelines` object is keyed by a user-chosen name:

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
| `token_var` | string | No | Env var name for the publish token. Each type has a default. |
| `username_var` | string | No | Env var for username auth (docker only). |
| `password_var` | string | No | Env var for password auth (docker only). |
| `assets` | bool | No | Enable building and uploading target-specific artifacts to GitHub Releases. |
| `max_asset_size_mb` | int | When `assets` or `custom_assets` is set | Maximum artifact size in MB. Release fails if any artifact exceeds this. |
| `custom_assets` | array | No | List of custom build artifacts. Each entry: `{name, build}`. |

## Pipeline types

:-: table-pipelines

## Class hierarchy

All pipeline implementations inherit from `BasePipeline`, which provides no-op defaults for publish and build steps plus the shared `build_custom_assets()` implementation.

| Class | Auth pattern | Pipelines |
| --- | --- | --- |
| `BasePipeline` | None (direct subclass) | go (proxy notification), maven (flexible auth), cloudflare-pages (selfdoc CLI) |
| `TokenPipeline(BasePipeline)` | Single env var token | npm, pypi, cargo, deno, hex |
| `CredentialPipeline(BasePipeline)` | Username + password env vars | docker |

`TokenPipeline` validates that the token env var is set before attempting publish and passes it to the ecosystem-specific publish command. `CredentialPipeline` validates both username and password env vars.

## Custom assets

Custom assets allow attaching arbitrary build artifacts to GitHub Releases. The flow during `rlsbl release run`:

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

Pipeline steps are gated on target capabilities:

| Capability | Effect when absent |
| --- | --- |
| `publish` | The publish step is skipped entirely for that target |
| `build_assets` | Asset building is skipped for that target |

This means a target that does not support publishing (e.g., a documentation-only target) will not attempt to run any pipeline's publish step, even if pipelines are configured. The pipeline config remains valid — it simply has no effect for that target.

## Migration from old publish key

The old `publish` key in `.rlsbl/config.json` is no longer recognized. Running `rlsbl release run` with a `publish` key present produces a **hard error** — no fallback, no deprecation warning.

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
- **Publish command:** Detects which package manager the project uses (npm, pnpm, or yarn) and runs the corresponding publish command (`npm publish`, `pnpm publish`, or `yarn npm publish`).
- **CI template:** Generates the appropriate install and publish steps for the detected package manager.
- **Quirks:** Package manager detection is based on lockfile presence (`package-lock.json` for npm, `pnpm-lock.yaml` for pnpm, `yarn.lock` for yarn). If multiple lockfiles exist, priority order is pnpm > yarn > npm.

### pypi

- **Class:** `TokenPipeline`
- **Default token env var:** `PYPI_TOKEN` (fallback: `TWINE_PASSWORD`)
- **Auth pattern:** Dual-token fallback. Checks `PYPI_TOKEN` first, then `TWINE_PASSWORD`. However, the preferred approach is OIDC Trusted Publishing, which requires no token at all — CI authenticates via GitHub's OIDC provider and `pypa/gh-action-pypi-publish`.
- **Publish command:** `uv publish` (preferred) or `twine upload dist/*` as fallback.
- **CI template:** Uses `pypa/gh-action-pypi-publish` with `id-token: write` permission for OIDC.
- **Quirks:** For new packages, a pending publisher must be configured on pypi.org before the first release. No local `uv publish` or token needed when using Trusted Publishing.

### go

- **Class:** `BasePipeline` (no token required)
- **Default token env var:** None
- **Auth pattern:** No authentication. Go modules are published by pushing a tagged commit — the Go module proxy picks it up automatically.
- **Publish command:** Notifies the Go module proxy (`proxy.golang.org`) by requesting the module at the new version. Optionally runs `go install` for binary projects.
- **CI template:** Minimal — Go publish is just the tag push plus a proxy notification step.
- **Quirks:** Reads the module path from `go.mod` to construct the proxy notification URL. Binary projects (those with a `main` package) can optionally trigger a `go install` verification step.

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
- **Default token env var:** Configurable; defaults to `MAVEN_TOKEN` or `GITHUB_TOKEN` depending on the target registry.
- **Auth pattern:** Flexible — supports token-based auth for GitHub Packages and credential-based auth for Maven Central. Auth is typically configured in `~/.m2/settings.xml` or passed via environment variables.
- **Publish command:** Detects gradle vs maven build system. Runs `./gradlew publish` for Gradle projects or `mvn deploy` for Maven projects.
- **CI template:** Generates appropriate publish steps based on detected build system and target registry.
- **Quirks:** Build system detection is based on the presence of `build.gradle`, `build.gradle.kts` (Gradle), or `pom.xml` (Maven). Subclasses `BasePipeline` directly rather than `TokenPipeline` because auth configuration varies significantly between registries.

### docker

- **Class:** `CredentialPipeline`
- **Default token env var:** `DOCKER_USERNAME` + `DOCKER_PASSWORD`
- **Auth pattern:** Username and password pair. Both must be set. Configured via `username_var` and `password_var` in the pipeline config.
- **Publish command:** `docker login` with the credentials, then `docker push` with the configured image and tag.
- **CI template:** Login step followed by build and push steps.
- **Quirks:** Requires `image` and `registry` fields in the pipeline config to construct the full image reference. The image is tagged with the release version automatically.

### cloudflare-pages

- **Class:** `BasePipeline`
- **Default token env var:** None for local deploys (uses ambient auth via selfdoc CLI or Wrangler).
- **Auth pattern:** No token needed for local deploys. Uses `CF_PAGES_API_TOKEN` and `CF_ACCOUNT_ID` from the environment when publishing locally via post-release hooks.
- **Publish command:** Wraps `selfdoc deploy` or Wrangler CLI for direct Cloudflare Pages deployment.
- **CI template:** Minimal — most Cloudflare Pages projects deploy locally from post-release hooks rather than CI.
- **Quirks:** The simplest pipeline implementation. Primarily used for documentation sites that deploy alongside library releases. No registry authentication dance — just a deploy command.
