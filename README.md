<p align="center">
  <img src="logo.svg" alt="rlsbl" width="336" height="105">
</p>

# rlsbl

Release orchestration and project scaffolding CLI for npm, PyPI, and Go. Pure Python, one dependency (tomlkit).

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
rlsbl release minor     # bump, tag, push, create GitHub Release
rlsbl watch <sha>       # monitor CI for that release
```

## Commands

All commands auto-detect registries from project files (`package.json`, `pyproject.toml`, `go.mod`). Use `--target <npm|pypi|go>` to target a specific one.

| Command | Description |
|---------|-------------|
| `release [patch\|minor\|major]` | Bump version, commit, tag, push, create GitHub Release |
| `scaffold [--force] [--update]` | Scaffold CI/CD, hooks, and release infrastructure |
| `status` | Show version, branch, last tag, changelog coverage, CI presence |
| `check <name>` | Check name availability on npm/PyPI/GitHub (parallel variant queries) |
| `config [show\|init\|migrate\|status]` | Manage project configuration and schema migrations |
| `undo [--yes]` | Revert the last release (tag, commit, GitHub Release) |
| `discover [--mine]` | List rlsbl ecosystem projects via GitHub topic search |
| `watch [<sha>]` | Monitor CI runs for a commit (parallel polling), print workflow audit summary, notify on completion |
| `unreleased [--json]` | List commits since last tag, report changelog coverage |
| `prs` | List open pull requests for the current repo |
| `targets` | List available release targets with detection status |
| `record-gif` | Record a demo GIF with vhs |
| `pre-push-check` | Verify CHANGELOG entry exists for the current version |

Global flags: `--help`, `--version`, `--target <npm|pypi|go>`, `--no-tag`.

## Release flow

When you run `rlsbl release [patch|minor|major]`:

1. Verifies `gh` CLI is installed and authenticated
2. Checks working tree is clean
3. Fetches origin and verifies local branch is not behind remote (use `--skip-remote-check` for offline releases)
4. Reads the current version from the primary project file
5. Computes the new version; confirms the tag does not already exist
6. Validates `CHANGELOG.md` contains a `## <new-version>` section
7. Runs `.rlsbl/hooks/pre-release.sh` if present (non-zero exit aborts; receives `RLSBL_VERSION`)
8. Acquires advisory lockfile (`.rlsbl/lock`) to prevent concurrent operations
9. Writes the new version to all detected project files and `.rlsbl/version`
10. Adds `rlsbl` keyword to manifests if ecosystem tagging is enabled
11. Verifies no unexpected files were modified (race condition guard)
12. Commits the version bump (uses `safegit` if available)
13. Tags and pushes to `origin`
14. Creates a GitHub Release with the changelog entry as notes
15. Adds `rlsbl` topic to the GitHub repo (if tagging enabled)
16. Runs secondary release targets (e.g., docs via selfdoc); use `--include`/`--exclude` to control
17. Runs `.rlsbl/hooks/post-release.sh` if present (non-fatal; receives `RLSBL_VERSION`)
18. Prints `Watch CI: rlsbl watch <sha>`

Use `--dry-run` to preview without changes. Use `--yes` for non-interactive mode (CI, AI agents). Use `--skip-remote-check` for offline releases.

**Multi-target releases:** Secondary targets (e.g., docs) run automatically during release. Control which targets run with `--include <target>` and `--exclude <target>` (comma-separated). Configure defaults in `.rlsbl/config.json` via the `release_targets` list. Use `rlsbl targets` to see all available targets.

**Documentation:** Use [selfdoc](https://github.com/smm-h/selfdoc) for documentation generation. rlsbl auto-triggers selfdoc during releases when `selfdoc.json` is detected.

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

## Config management

Schema-driven configuration migration system for projects that ship user-facing config files.

```
rlsbl config init      # scaffold config migration infrastructure
rlsbl config migrate   # run pending migrations
rlsbl config status    # show migration status
rlsbl config show      # show resolved project configuration
```

`config init` creates `.rlsbl/config-schema.json` and a `defaults/` directory. Migrations support deep merge, flat merge, and list-by-key merge strategies with versioned schema tracking and atomic writes.

### Library API

```python
from rlsbl.lib import ConfigMigrator, load_schema, migrate

# One-liner: load schema and run all pending migrations
result = migrate(".")  # returns {filename: was_written} or None
```

## Undo

```
rlsbl undo         # interactive: confirms before each destructive step
rlsbl undo --yes   # non-interactive: auto-confirms, auto-pushes
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
| PyPI | Run `uv publish` once, then set up [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) | CI publishes via OIDC |
| Go | Push tag -- Go modules are published by the tag itself | `pkg.go.dev` indexes automatically |

## Requirements

- Python 3.11+
- [GitHub CLI](https://cli.github.com) (`gh`), installed and authenticated
- git
- Node 24+ (for npm CI/publish templates)

## License

MIT
