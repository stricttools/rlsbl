---
description: "Configuration reference for rlsbl: config.json project settings with pipelines and batch limits, workspace.toml for monorepos, and selfdoc.json for documentation."
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
| release_branches | array | Branch names that trigger the manual-release-push warning. Default: `["main", "master"]` when absent. An empty list is a hard error -- either omit the key entirely or list at least one branch. |
| changelog_format | string | Controls the format of generated CHANGELOG.md. Default: `"grouped"`. Currently the only supported value, which produces version sections with `### Breaking`, `### Features`, `### Fixes` sub-headers. |
| batch_limits | object | Limits and exclusions for changelog batch-size validation checks. See [batch_limits](#batch_limits) below. |

Configuration precedence for tagging: CLI flag (`--no-tag`) > project config > user config (`~/.rlsbl/config.json`) > default (true).

### batch_limits

The `batch_limits` object controls the `batch_size_commits` and `batch_size_entries` changelog validation checks, which prevent excessively large changelog entries that obscure individual change attribution. Both checks produce blocking errors when they fail, and violations must be resolved before releasing. Default limits allow a maximum of 5 commits per entry and 5 entries per commit.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `max_commits_per_entry` | int | 5 | Maximum number of commit hashes allowed in a single JSONL entry. Entries exceeding this limit fail the `batch_size_commits` check. |
| `max_entries_per_commit` | int | 5 | Maximum number of JSONL entries that may reference the same commit hash. Commits exceeding this limit fail the `batch_size_entries` check. |
| `exclusions` | array | `[]` | Per-violation silencers for known exceptions (see below). |

Each entry in `exclusions` is a dict with:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `reason` | string | Yes | Mandatory audit trail explaining why this exclusion exists. |
| `commits` | array | At least one of `commits` or `entries` | Commit hashes to exclude from the `batch_size_entries` check. |
| `entries` | array | At least one of `commits` or `entries` | Entry identifiers (commit lists) to exclude from the `batch_size_commits` check. |

Example:

```json
{
  "batch_limits": {
    "max_commits_per_entry": 5,
    "max_entries_per_commit": 5,
    "exclusions": [
      {
        "reason": "Large refactor commit touches many changelog areas",
        "commits": ["a1b2c3d"]
      }
    ]
  }
}
```

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

#### Per-pipeline-type reference

| Type | Default token_var | Auth pattern | Publish action |
| --- | --- | --- | --- |
| `npm` | `NPM_TOKEN` | Token | Runs `npm publish` (or pnpm/yarn equivalent based on lockfile detection) |
| `pypi` | `PYPI_TOKEN` (or `TWINE_PASSWORD`) | Token / OIDC | OIDC Trusted Publishing preferred; falls back to token-based upload via twine |
| `go` | None | None | Notifies Go module proxy (`GOPROXY=proxy.golang.org`); no authentication required |
| `cargo` | `CARGO_REGISTRY_TOKEN` | Token | Runs `cargo publish` to crates.io |
| `deno` | `DENO_TOKEN` (or `JSR_TOKEN`) | Token | Runs `deno publish` to JSR |
| `hex` | `HEX_API_KEY` | Token | Runs `mix hex.publish` to hex.pm |
| `maven` | `MAVEN_TOKEN` (or `GITHUB_TOKEN`) | Token | Runs gradle or maven publish task to configured repository |
| `docker` | N/A | Username + Password | Authenticates via `DOCKER_USERNAME` + `DOCKER_PASSWORD`, then runs `docker push` |
| `cloudflare-pages` | None | Selfdoc CLI | Uses the selfdoc CLI for deploy; no token needed locally (CF credentials sourced from env) |

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

## User-level configuration

The user-level configuration file at `~/.rlsbl/config.json` provides personal defaults that apply across all rlsbl-managed projects on the machine. It uses the same JSON schema as the project-level config, but with lower precedence -- project-level settings always override user-level ones, and CLI flags override both.

Location: `~/.rlsbl/config.json`

This optional file uses the same JSON format as the project-level `.rlsbl/config.json`. It provides personal defaults that apply to all rlsbl-managed projects when a given key is not set at the project level.

**Precedence (highest to lowest):**

1. CLI flags (e.g., `--no-tag`)
2. Project config (`.rlsbl/config.json`)
3. User config (`~/.rlsbl/config.json`)
4. Built-in defaults

The file is optional — a missing `~/.rlsbl/config.json` is not an error. When absent, built-in defaults apply for any key not set at the project level.

Currently supported keys:

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `tag` | bool | `true` | Controls whether ecosystem tags (e.g., npm dist-tags) are created during release. |

Example `~/.rlsbl/config.json`:

```json
{
  "tag": false
}
```

## CLI flag overrides

Some CLI flags override config.json keys for a single invocation, providing temporary behavior changes without modifying the persistent configuration. Most global flags like `--dry-run`, `--yes`, and `--quiet` are runtime-only and have no persistent config equivalent, affecting only the current command execution.

| Flag | Config key | Scope | Effect |
| --- | --- | --- | --- |
| `--no-tag` | `tag` | project + user | Disables ecosystem tagging for this invocation |
| `--allow-dirty` | (none) | release only | Skips clean working tree check |
| `--watch`/`--no-watch` | (none) | release only | Controls CI monitoring after push |
| `RLSBL_PUSH_TIMEOUT` env | `push_timeout` | project | Push timeout in seconds (default 120) |

Global flags `--dry-run`, `--yes`, and `--quiet` are runtime-only and have no `config.json` equivalent. They affect the current invocation but are never persisted to configuration.

## .rlsbl-monorepo/workspace.toml

Monorepo workspace definition that lists all sub-projects, their relative paths, and optional names. rlsbl walks up from the current directory to find this file, so you can run release commands from within any sub-project. See the [monorepo guide](monorepo.md) for setup instructions, workspace commands, and subtree publishing.

The file uses TOML format with a `[[projects]]` array. Each entry has a `path` key (relative to the monorepo root) and an optional `name` key. If `name` is omitted, the directory basename is used.

## selfdoc.json

When present in the project root, this file configures documentation builds via selfdoc. It specifies the source directories to scan, the output path for generated pages, the base URL for the published site, and an optional deploy provider such as Cloudflare Pages. Documentation deployment is handled via a `cloudflare-pages` pipeline in `.rlsbl/config.json`, not as a release target. See the [selfdoc documentation](https://github.com/smm-h/selfdoc) for the full schema.

:-: table-schema path="selfdoc.json" exclude="version,versions"
