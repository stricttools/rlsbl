---
title: CLAUDE.md
---
# rlsbl

:-: var key="project.description"

Built in Python 3.11+ with ruamel-yaml, tomlkit, strictcli, and tree-sitter. Also distributed as an npm wrapper package. Current version: check `package.json`.

## Commands

:-: table-commands path="rlsbl/"

## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Run `rlsbl release init` to scaffold `.rlsbl/releases/unreleased.toml`
- Edit the release file: set bump type (patch/minor/major), include/exclude targets
- Run `rlsbl release run` to read the release file, bump version, and create a GitHub Release
- CI handles publishing automatically via the publish workflow
- Never publish manually -- always use `rlsbl release run`
- Requires NPM_TOKEN secret on GitHub (Settings > Secrets > Actions)
- Use `rlsbl release run --dry-run` to preview a release without making changes
- Global flags `--dry-run`, `--yes`, `--quiet` are available on all commands
- Only `--allow-dirty` remains as a release-specific flag

## Release pipeline order

During `rlsbl release run`, the validation and build steps run in this order:

1. Pre-checks hook (`.rlsbl/hooks/pre-checks.sh`)
2. Strictcli schema dump (`--dump-schema`, for projects using strictcli)
3. Selfdoc gen (`selfdoc gen --no-commit`, regenerates docs from source)
4. Selfdoc check (verifies generated docs are up-to-date)
5. Built-in tests (`uv run pytest`, `go test`, `npm test`) -- skipped if pre-release hook is customized
6. Built-in lint (library projects only) -- skipped if pre-release hook is customized
7. Pre-release hook (`.rlsbl/hooks/pre-release.sh`)

**Hooks override:** When the pre-release hook has been customized (its content differs from any known scaffold template version, determined by content hash comparison), built-in tests and lint are skipped. The hook is expected to handle testing and linting itself. An unmodified scaffold template or a missing hook file is considered "effectively empty" and does not trigger the override -- built-in tests and lint run normally.

## Conventions

- No tokens or secrets in command-line arguments (use env vars or config files)
- All file writes to shared state should be atomic (write to tmp, then rename)
- External calls (APIs, CLI tools) must have timeouts and graceful fallbacks
- Use `rlsbl dev install` for local editable installs (it picks the right tool per target: `uv tool install -e` for pypi, `npm link` for npm, `go install` for go, etc.)
- CI runs smoke tests on every push; manual testing for UI/UX changes

## Configuration

`.rlsbl/config.json` holds per-project settings. Notable keys:

- `pipelines` -- publish pipelines keyed by user-chosen name (replaces the old `publish` key, which is now rejected during `rlsbl release run`). Each entry is a dict with mandatory `type` (string) and `local` (bool) fields, plus type-specific optional fields. See [Pipelines](#pipelines) below.
- `private` (bool, required) -- safety guardrail for private repositories. When `true`, blocks publishing to public registries (npm, PyPI, etc.). Must be explicitly set in `config.json` -- there is no default.
- `release_branches` -- list of branch names that trigger the manual-release-push warning. Defaults to `["main", "master"]` when the key is absent. An empty list is now an error -- either remove the key or list at least one branch.
- `batch_limits` -- limits and exclusions for the `batch_size_commits` and `batch_size_entries` changelog validation checks. Both checks are blocking errors when they fail. Keys:
    - `max_commits_per_entry` (int, default `5`) -- maximum number of commit hashes allowed in a single JSONL entry.
    - `max_entries_per_commit` (int, default `5`) -- maximum number of JSONL entries that may reference the same commit hash.
    - `exclusions` (list of dicts, default `[]`) -- per-violation silencers. Each exclusion must have a `reason` (string, mandatory audit trail) plus at least one of `commits` or `entries`.

## Pipelines

Pipelines handle publishing and are configured separately from targets (which handle versioning). A project's targets determine which files get version-bumped; its pipelines determine where and how the release is published. This separation means a project can have, for example, an npm target for versioning but a cloudflare-pages pipeline for publishing.

The `pipelines` key in `.rlsbl/config.json` is a dict where each key is a user-chosen pipeline name and each value has:

- `type` (string, required) -- one of the 9 built-in pipeline types: `npm`, `pypi`, `go`, `cargo`, `deno`, `hex`, `maven`, `docker`, `cloudflare-pages`
- `local` (bool, required) -- whether to publish from the developer machine. When `false`, the local publish step is skipped and CI handles it.
- `token_var` (string, optional) -- name of the env var holding the publish token. Used by token-based pipelines (npm, pypi, hex, deno, maven, cargo, go). Each type has a default (e.g. `NPM_TOKEN` for npm).
- `username_var` / `password_var` (string, optional) -- name of the env vars for username/password auth. Used by credential-based pipelines (docker).
- `assets` (bool, optional) -- enables building and uploading target-specific artifacts to GitHub Releases.
- `max_asset_size_mb` (int, required when `assets` is true or `custom_assets` is present) -- maximum artifact size in MB. Releases fail if any artifact exceeds this limit.
- `custom_assets` (list, optional) -- list of custom build artifacts to attach to GitHub Releases. Each entry has `name` (string, output filename in `$RLSBL_DIST_DIR`) and `build` (string, shell command to execute). The build command receives `$RLSBL_DIST_DIR` as an env var pointing to the distribution directory. Requires `max_asset_size_mb`.

Example:

```json
{
  "pipelines": {
    "npm-publish": {
      "type": "npm",
      "local": false
    },
    "docs-deploy": {
      "type": "cloudflare-pages",
      "local": true
    }
  }
}
```

The old `publish` key in `.rlsbl/config.json` is no longer recognized. Running `rlsbl release run` with a `publish` key present is a hard error -- migrate to `pipelines`.

## Check system

30 checks registered across 5 tags: `project`, `release`, `changelog`, `workspace`, `quality`. Run individual checks or by tag:

- `rlsbl check --all` -- run all checks
- `rlsbl check --tag changelog` -- run all changelog validation checks (replaces the removed `rlsbl changelog validate`)
- `rlsbl check --tag workspace` -- run all workspace checks (replaces `monorepo lint`)
- `rlsbl check --name version-consistency` -- run a single check by name

## CI customization

Add custom GitHub Actions jobs via the user-owned `.github/workflows/ci-custom.yml` and `publish-custom.yml` files. Scaffold never touches them, so they survive `scaffold`'s three-way merge. See `docs/ci-customization.md` for the pattern.

## Pre-push hook

`.git/hooks/pre-push` runs `rlsbl pre-push-check`, which:

- enforces JSONL commit coverage for every pushed commit (hard error -- blocks the push)
- warns when a push targets a release branch but did not originate from `rlsbl release run` / `rlsbl release undo`

The release/undo commands set `RLSBL_RELEASE_PUSH=1` in the push environment so the hook recognises legitimate release pushes and suppresses the warning. Users should not set this env var directly -- it is an internal contract between rlsbl and its own git hook.

## Validation cache

`rlsbl check --tag changelog` writes `.rlsbl/changes/.validated` (the SHA of the last successfully validated HEAD) so subsequent invocations can short-circuit when nothing has changed. The cache file is auto-committed with an `Autogenerated: true` trailer so it persists across sessions and machines. That auto-commit is exempted from changelog coverage requirements, but it still shows up in `git log`, which is why `rlsbl status` may report "1 commit behind v0.X.0" immediately after a release -- this is by design, not a bug. The next release recognises the trailer and skips the entry requirement automatically.

## Private repos and asset uploads

Private repositories set `"private": true` in `.rlsbl/config.json` to block accidental publishing to public registries. Asset upload (building and attaching artifacts to GitHub Releases) is configured per-pipeline via `pipelines.<name>.assets` and `pipelines.<name>.max_asset_size_mb`. Custom build artifacts use the `custom_assets` list on the pipeline config (see [Pipelines](#pipelines)). The `private-hook-stale` check detects leftover private hook files and warns that they should be deleted.

## Strictcli schema

Projects that use strictcli as their CLI framework automatically run `--dump-schema` during the release pipeline (after pre-checks, before tests). This ensures the CLI schema file is always up-to-date in the release commit. No manual configuration is needed -- rlsbl detects strictcli usage and adds the step.

## Monorepo

Monorepos use `.rlsbl-monorepo/` with a `workspace.toml` listing sub-projects. Supports architectural layer rules via `[layers]` in `workspace.toml` for enforcing dependency direction. Use `monorepo graph` for dependency visualization, `monorepo snapshot` for committed JSON artifacts, `monorepo impact` for change analysis, and `monorepo release` for batch releases in topological order.
