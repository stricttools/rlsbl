---
description: "Configuration reference for rlsbl: config.json settings, workspace.toml for monorepos, and selfdoc.json for documentation."
---

# Configuration reference

## .rlsbl/config.json

Project-level configuration file created by `rlsbl config init` or `rlsbl scaffold`. This JSON file controls release behavior such as which targets to use (chosen from 18 supported registries) and whether ecosystem tagging is enabled. Settings here override user-level defaults in `~/.rlsbl/config.json` but are themselves overridden by CLI flags passed at release time, forming a 3-layer precedence chain (CLI > project > user).

:-: table-config path=".rlsbl/config.json"

### Key reference

| Key | Type | Description |
| --- | ---- | ----------- |
| targets | array | List of target names to use for versioning (overrides auto-detection) |
| pipelines | object | Publish pipelines keyed by user-chosen name (see [Pipeline config](#pipeline-config) below) |
| tag | bool | Enable/disable ecosystem tagging (default: true) |
| private | bool (required) | Safety guardrail: when true, blocks publishing to public registries |

Configuration precedence for tagging: CLI flag (`--no-tag`) > project config > user config (`~/.rlsbl/config.json`) > default (true).

### Pipeline config

The `pipelines` key configures how releases are published. It replaces the old `publish` key, which is now rejected during `rlsbl release run`. Pipelines are separate from targets: targets handle version bumps (which files to update), while pipelines handle publishing (where and how to distribute the release).

Each entry in `pipelines` is keyed by a user-chosen name and must have:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `type` | string | Yes | One of the 9 built-in pipeline types (see below) |
| `local` | bool | Yes | Whether to publish from the developer machine. When `false`, CI handles publishing. |
| `token_var` | string | No | Env var name for the publish token. Each type has a default (e.g. `NPM_TOKEN` for npm). |
| `username_var` | string | No | Env var name for username auth (used by docker). |
| `password_var` | string | No | Env var name for password auth (used by docker). |
| `assets` | bool | No | Enable building and uploading target-specific artifacts to GitHub Releases. |
| `max_asset_size_mb` | int | When `assets` or `custom_assets` is set | Maximum artifact size in MB. Releases fail if exceeded. |
| `custom_assets` | array | No | List of custom build artifacts (see below). |

**Built-in pipeline types:** `npm`, `pypi`, `go`, `cargo`, `deno`, `hex`, `maven`, `docker`, `cloudflare-pages`

Pipeline types use different auth patterns:
- **Token-based** (npm, pypi, go, cargo, deno, hex, maven): authenticate via a single env var specified by `token_var`
- **Credential-based** (docker): authenticate via `username_var` and `password_var`
- **Other** (cloudflare-pages): type-specific auth configured per pipeline

#### custom_assets

The `custom_assets` field is a list of dicts, each with:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | Yes | Output filename (must appear in `$RLSBL_DIST_DIR` after the build command runs) |
| `build` | string | Yes | Shell command to execute. Receives `$RLSBL_DIST_DIR` env var pointing to the distribution directory. |

The build command runs with `$RLSBL_DIST_DIR` set to a temporary distribution directory. The command must produce a file named `name` in that directory. After building, rlsbl verifies the file exists and checks its size against `max_asset_size_mb`. All custom asset files are then uploaded to the GitHub Release.

Example config:

```json
{
  "pipelines": {
    "npm-publish": {
      "type": "npm",
      "local": false
    },
    "docker-push": {
      "type": "docker",
      "local": true,
      "username_var": "DOCKER_USER",
      "password_var": "DOCKER_PASS"
    },
    "binaries": {
      "type": "go",
      "local": false,
      "custom_assets": [
        {"name": "myapp-linux-amd64.tar.gz", "build": "make dist-linux"},
        {"name": "myapp-darwin-arm64.tar.gz", "build": "make dist-darwin"}
      ],
      "max_asset_size_mb": 50
    }
  }
}
```

:-: ref path="rlsbl.config"

## .rlsbl-monorepo/workspace.toml

Monorepo workspace definition that lists all sub-projects, their relative paths, and optional names. rlsbl walks up from the current directory to find this file, so you can run release commands from within any sub-project. See the [monorepo guide](monorepo.md) for setup instructions, workspace commands, and subtree publishing.

The file uses TOML format with a `[[projects]]` array. Each entry has a `path` key (relative to the monorepo root) and an optional `name` key. If `name` is omitted, the directory basename is used.

## selfdoc.json

When present in the project root, this file configures documentation builds via selfdoc. It specifies the source directories to scan, the output path for generated pages, the base URL for the published site, and an optional deploy provider such as Cloudflare Pages. Documentation deployment is handled via a `cloudflare-pages` pipeline in `.rlsbl/config.json`, not as a release target. See the [selfdoc documentation](https://github.com/smm-h/selfdoc) for the full schema.

:-: table-schema path="selfdoc.json" exclude="version,versions"
