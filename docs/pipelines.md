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
