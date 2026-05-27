---
title: README.md
---
<p align="center">
  <img src="logo.svg" alt="rlsbl" width="336" height="105">
</p>

# rlsbl

:-: var key="project.description"

## Install

From PyPI:

```
uv tool install rlsbl
```

From npm (wrapper):

```
npm i -g rlsbl
```

## Quick start

```
rlsbl scaffold          # set up CI/CD, hooks, changelog
# ... develop, commit ...
rlsbl release init      # scaffold .rlsbl/releases/unreleased.toml
# ... edit bump type, targets ...
rlsbl release run       # bump, tag, push, create GitHub Release
rlsbl watch <sha>       # monitor CI for that release
```

## Commands

All commands auto-detect registries from project files (`package.json`, `pyproject.toml`, `go.mod`). Use `--target <npm|pypi|go>` to target a specific one.

:-: table-commands path="rlsbl/"

Global flags: `--help`, `--version`, `--dry-run`, `--yes`, `--quiet`.

## Release flow

When you run `rlsbl release run`:

1. Reads `.rlsbl/releases/unreleased.toml` for bump type (patch/minor/major) and target selection
2. Verifies `gh` CLI is installed and authenticated
3. Checks working tree is clean (use `--allow-dirty` to override)
4. Fetches origin and verifies local branch is not behind remote
5. Reads the current version from the primary project file
6. Computes the new version; confirms the tag does not already exist
7. Validates JSONL changelog via the check system
8. Runs `.rlsbl/hooks/pre-checks.sh` if present (user-owned, non-zero aborts)
9. Runs built-in tests and lint
10. Runs `.rlsbl/hooks/pre-release.sh` if present (scaffold-managed, non-zero aborts)
11. Acquires advisory lockfile (`.rlsbl/lock`) to prevent concurrent operations
12. Writes the new version to all detected project files and `.rlsbl/version`
13. Commits the version bump (uses `safegit` if available)
14. Tags and pushes to `origin`
15. Finalizes JSONL changelog (renames `unreleased.jsonl`, generates CHANGELOG.md)
16. Creates a GitHub Release with the changelog entry as notes
17. Runs secondary release targets (e.g., docs via selfdoc)
18. Runs `.rlsbl/hooks/post-release.sh` if present (non-fatal)
19. Prints `Watch CI: rlsbl watch <sha>`

Use `--dry-run` to preview without changes. Use `--yes` for non-interactive mode (CI, AI agents).

Create the release file with `rlsbl release init`, which auto-detects project targets and scaffolds the TOML file.

First release: if the current version has never been tagged, `release` publishes it as-is (bump type is ignored).

Pre-release versions (e.g. `1.0.0-beta.1`) are supported.

## Scaffold

```
rlsbl scaffold              # create CI/CD for all detected registries
rlsbl scaffold --update     # three-way merge template updates with user customizations
rlsbl scaffold --force      # overwrite managed files (user-owned files still preserved)
rlsbl scaffold --no-commit  # skip auto-commit of scaffolded files
```

Created files are committed automatically by default.

| File | Purpose |
|------|---------|
| `.github/workflows/ci.yml` | CI workflow (lint, test) |
| `.github/workflows/publish.yml` | Publish on GitHub Release (OIDC) |
| `CHANGELOG.md` | Version changelog |
| `LICENSE` | MIT license (author and year filled in) |
| `.gitignore` | Standard ignores for the ecosystem |
| `CLAUDE.md` | AI assistant instructions |
| `.claude/settings.json` | Claude Code settings |
| `.rlsbl/hooks/pre-checks.sh` | User-customizable pre-checks validation |
| `.rlsbl/hooks/pre-release.sh` | User-customizable pre-release validation |
| `.rlsbl/hooks/post-release.sh` | User-customizable post-release actions |
| `.git/hooks/pre-push` | One-liner: `exec rlsbl pre-push-check "$@"` |
| `.rlsbl/bases/` | Three-way merge bases for `--update` |

**Three-way merge (`--update`):** Bases are stored at scaffold time. On `--update`, user customizations and template updates merge via `git merge-file`. Conflicts get git-style conflict markers.

**User-owned files** (CHANGELOG.md, LICENSE, hooks) are never overwritten, even with `--force`.

**Customizing CI without conflicts:** Instead of editing `ci.yml` or `publish.yml` (which can produce merge conflicts on `--update`), put extra jobs in a separate workflow file scaffold never touches:

- `.github/workflows/ci-custom.yml` -- runs alongside `ci.yml`
- `.github/workflows/publish-custom.yml` -- runs alongside `publish.yml`

See [docs/ci-customization.md](docs/ci-customization.md) for an example.

**Runs config migrations** when `.rlsbl/config-schema.json` exists.

## Check system

30 project checks organized by tag:

| Tag | Checks | Description |
|-----|--------|-------------|
| `project` | 7 | Version, name, license, description consistency; lockfile presence; private hook staleness; config schema |
| `release` | 4 | Local/remote tag, GitHub Release, branch sync |
| `changelog` | 9 | Hash resolution, range, coverage, orphans, schema, user-facing, batch limits, entry |
| `workspace` | 5 | CI router, CI sync, targets, unregistered, stale entries |
| `quality` | 1 | Library lint |
| (untagged) | 4 | Layer violations, dependency validation (unused/undeclared/stale) |

```
rlsbl check --all              # run all 30 checks
rlsbl check --tag changelog    # run checks by tag
rlsbl check --name lock        # run a single check
```

## Config management

Schema-driven configuration migration system for projects that ship user-facing config files.

```
rlsbl migrate              # run pending migrations
rlsbl migrate --status     # show migration status
rlsbl migrate --dry-run    # preview changes
```

### Library API

```python
from rlsbl.lib import ConfigMigrator, load_schema, migrate

# One-liner: load schema and run all pending migrations
result = migrate(".")  # returns {filename: was_written} or None
```

## Undo

```
rlsbl release undo         # interactive: confirms before each destructive step
rlsbl release undo --yes   # non-interactive: auto-confirms, auto-pushes
```

Reverts the last release:

1. Deletes the GitHub Release
2. Deletes the git tag (remote + local)
3. Reverts the version bump commit (if HEAD matches the tag)
4. Pushes the revert commit (with confirmation, or automatic with `--yes`)

On partial failure, prints a structured summary table with remediation commands for each failed step.

## Pre-push hook

The `.git/hooks/pre-push` hook calls `rlsbl pre-push-check`, which:

1. Detects project type (`package.json`, `pyproject.toml`, or `VERSION`)
2. Extracts the current version
3. Checks that `CHANGELOG.md` contains a `## <version>` heading
4. Blocks the push if the entry is missing

To reinstall manually:

```
echo '#!/bin/sh' > .git/hooks/pre-push && echo 'exec rlsbl pre-push-check "$@"' >> .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

## Ecosystem tagging

`scaffold` and `release` add an `"rlsbl"` keyword to project manifests and set the `rlsbl` topic on the GitHub repository, making projects discoverable via `rlsbl discover`.

To disable:

| Method | Scope |
|--------|-------|
| `--no-tag` flag | Single invocation |
| `{"tag": false}` in `.rlsbl/config.json` | This project |
| `{"tag": false}` in `~/.rlsbl/config.json` | All projects |

## Monorepo

Manage multi-package workspaces with `rlsbl monorepo`:

- `monorepo init` / `monorepo add` / `monorepo remove` -- workspace management
- `monorepo sync` -- synchronize CI workflows
- `monorepo graph` -- export dependency graph (JSON, DOT, text)
- `monorepo snapshot` -- committed JSON artifact of workspace state
- `monorepo impact` -- change analysis across the dependency graph
- `monorepo release` -- batch release in topological order

Supports architectural layer rules via `[layers]` in `workspace.toml` for enforcing dependency direction.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RLSBL_PUSH_TIMEOUT` | `120` | Timeout in seconds for `git push` operations |
| `RLSBL_VERSION` | -- | Set when running pre-release and post-release hooks; contains the version being released |
| `GITHUB_TOKEN` | -- | Used by `gh` CLI for GitHub API calls; `discover` works unauthenticated for public repos |

## First publish

| Registry | Setup | Then |
|----------|-------|------|
| npm | Add `NPM_TOKEN` secret to GitHub repo (Settings > Secrets > Actions) | CI publishes on GitHub Release |
| PyPI | Set up [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no tokens needed) | CI publishes via OIDC |
| Go | Push tag -- Go modules are published by the tag itself | `pkg.go.dev` indexes automatically |

## Requirements

- Python 3.11+
- [GitHub CLI](https://cli.github.com) (`gh`), installed and authenticated
- git
- Node 24+ (for npm CI/publish templates)

## License

MIT
