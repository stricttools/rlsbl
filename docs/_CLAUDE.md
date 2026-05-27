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
5. Built-in tests (`uv run pytest`, `go test`, `npm test`)
6. Built-in lint (library projects only)
7. Pre-release hook (`.rlsbl/hooks/pre-release.sh`)

## Conventions

- No tokens or secrets in command-line arguments (use env vars or config files)
- All file writes to shared state should be atomic (write to tmp, then rename)
- External calls (APIs, CLI tools) must have timeouts and graceful fallbacks
- Use `rlsbl dev install` for local editable installs (it picks the right tool per target: `uv tool install -e` for pypi, `npm link` for npm, `go install` for go, etc.)
- CI runs smoke tests on every push; manual testing for UI/UX changes

## Configuration

`.rlsbl/config.json` holds per-project settings. Notable keys:

- `publish` -- per-target publish configuration (e.g. `publish.pypi`, `publish.npm`, `publish.docker`). Each entry can set:
    - `local` (bool) -- whether to publish from the developer machine; when `false`, the local publish step is skipped and CI handles it.
    - `token_var` -- name of the env var holding the publish token (used by pypi, npm, hex, deno, maven, etc.)
    - `username_var` / `password_var` -- name of the env vars for username/password auth (used by docker)
    - `assets` (bool) -- enables building and uploading artifacts to GitHub Releases. When true, the release step builds target-specific artifacts and attaches them to the GitHub Release.
    - `max_asset_size_mb` (int, required when `assets` is true) -- maximum artifact size in MB. Releases fail if any artifact exceeds this limit.
- `private` (bool, required) -- safety guardrail for private repositories. When `true`, blocks publishing to public registries (npm, PyPI, etc.). Must be explicitly set in `config.json` -- there is no default.
- `release_branches` -- list of branch names that trigger the manual-release-push warning. Defaults to `["main", "master"]` when the key is absent. An empty list is now an error -- either remove the key or list at least one branch.
- `batch_limits` -- limits and exclusions for the `batch_size_commits` and `batch_size_entries` changelog validation checks. Both checks are blocking errors when they fail. Keys:
    - `max_commits_per_entry` (int, default `5`) -- maximum number of commit hashes allowed in a single JSONL entry.
    - `max_entries_per_commit` (int, default `5`) -- maximum number of JSONL entries that may reference the same commit hash.
    - `exclusions` (list of dicts, default `[]`) -- per-violation silencers. Each exclusion must have a `reason` (string, mandatory audit trail) plus at least one of `commits` or `entries`.

## Check system

30 checks registered across 5 tags: `project`, `release`, `changelog`, `workspace`, `quality`. Run individual checks or by tag:

- `rlsbl check --all` -- run all checks
- `rlsbl check --tag changelog` -- run all changelog validation checks (replaces the removed `rlsbl changelog validate`)
- `rlsbl check --tag workspace` -- run all workspace checks (replaces `monorepo lint`)
- `rlsbl check --name version-consistency` -- run a single check by name

## CI customization

Add custom GitHub Actions jobs via the user-owned `.github/workflows/ci-custom.yml` and `publish-custom.yml` files. Scaffold never touches them, so they survive `scaffold --update`'s three-way merge. See `docs/ci-customization.md` for the pattern.

## Pre-push hook

`.git/hooks/pre-push` runs `rlsbl pre-push-check`, which:

- enforces JSONL commit coverage for every pushed commit (hard error -- blocks the push)
- warns when a push targets a release branch but did not originate from `rlsbl release run` / `rlsbl release undo`

The release/undo commands set `RLSBL_RELEASE_PUSH=1` in the push environment so the hook recognises legitimate release pushes and suppresses the warning. Users should not set this env var directly -- it is an internal contract between rlsbl and its own git hook.

## Validation cache

`rlsbl check --tag changelog` writes `.rlsbl/changes/.validated` (the SHA of the last successfully validated HEAD) so subsequent invocations can short-circuit when nothing has changed. The cache file is auto-committed with an `Autogenerated: true` trailer so it persists across sessions and machines. That auto-commit is exempted from changelog coverage requirements, but it still shows up in `git log`, which is why `rlsbl status` may report "1 commit behind v0.X.0" immediately after a release -- this is by design, not a bug. The next release recognises the trailer and skips the entry requirement automatically.

## Private repos and asset uploads

Private repositories set `"private": true` in `.rlsbl/config.json` to block accidental publishing to public registries. Asset upload (building and attaching artifacts to GitHub Releases) is now a built-in release step configured via `publish.<target>.assets` and `publish.<target>.max_asset_size_mb` in the config. The old private hook template for asset uploads is removed -- the `private-hook-stale` check detects leftover private hook files and warns that they should be deleted in favor of the built-in asset configuration.

## Strictcli schema

Projects that use strictcli as their CLI framework automatically run `--dump-schema` during the release pipeline (after pre-checks, before tests). This ensures the CLI schema file is always up-to-date in the release commit. No manual configuration is needed -- rlsbl detects strictcli usage and adds the step.

## Monorepo

Monorepos use `.rlsbl-monorepo/` with a `workspace.toml` listing sub-projects. Supports architectural layer rules via `[layers]` in `workspace.toml` for enforcing dependency direction. Use `monorepo graph` for dependency visualization, `monorepo snapshot` for committed JSON artifacts, `monorepo impact` for change analysis, and `monorepo release` for batch releases in topological order.
