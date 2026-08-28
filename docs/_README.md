---
title: README.md
---
<p align="center">
  <img src="logo.svg" alt="rlsbl" width="336" height="105">
</p>

# rlsbl

:-: target-count

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
rlsbl scaffold          # set up CI/CD, hooks, changelog, pipelines
# ... develop, commit ...
rlsbl release init      # scaffold .rlsbl/releases/unreleased.toml
# ... edit bump type, targets, pipelines ...
rlsbl release run       # bump, push the candidate, wait for CI, tag, publish
rlsbl watch <sha>       # monitor CI for that release
```

## Commands

All commands auto-detect targets (versioning) from project files (`package.json`, `pyproject.toml`, `go.mod`) and pipelines (publishing) from `.rlsbl/config.json`. Targets handle version bumps; pipelines handle where releases are published.

:-: table-commands

Global flags: `--help`, `--version`, `--dry-run`, `--approve-consequential`, `--quiet`, `--verbose`.

## Release flow

`rlsbl release run` reads `.rlsbl/releases/unreleased.toml` for the bump type, the
description and the target selection, then:

1. Verifies `gh` auth and a clean working tree (`--allow-dirty` accepts a dirty one), computes the new version and confirms its tag does not exist
2. Validates the JSONL changelog and regenerates CHANGELOG.md from it
3. Runs `.rlsbl/hooks/pre-checks.sh` (user-owned), the strictcli schema dump, `selfdoc gen` and `selfdoc check`, the built-in tests and lint, and `.rlsbl/hooks/pre-release.sh` (scaffold-managed) -- any non-zero aborts
4. Writes the new version to every detected target file and `.rlsbl/version`, commits it with the tag string as the message, and pushes that commit **untagged**: the release candidate
5. Waits in-process for the repository's own push-triggered CI to conclude on that exact commit
6. Finalizes the changelog (renames `unreleased.jsonl` to the version's file, opens a fresh one, regenerates CHANGELOG.md), archives the release file, tags the **CI-verified commit**, pushes the finalization commits and the tags, and creates the GitHub Release with the version's changelog section as notes
7. Uploads assets, runs each pipeline's `publish` (configured in `.rlsbl/config.json`), deploys, runs `.rlsbl/hooks/post-release.sh` (non-fatal), and prints `Watch CI: rlsbl watch <sha>`

Everything above the candidate push is reversible; everything below it is not. A red
CI verdict therefore leaves nothing behind but a commit on the branch -- no tag, no
GitHub Release, no finalized changelog, nothing on any registry. Fix forward on the
release branch and `rlsbl release resume` completes the *same* version; a failed
release never burns it.

The step-by-step pipeline, including what each step does in monorepo and releasable
mode, is in [docs/release-workflow.md](docs/release-workflow.md).

Use `--dry-run` to preview without changes: mutating operations are recorded and printed as a
would-do log rather than performed. A small set of commands declares itself `consequential`
(`release run`/`resume`/`retry`/`undo`/`deprecate`/`yank`/`scrub`/`reconcile`, `claim-name`,
`deploy`, `monorepo release run`/`mirror`/`absorb`/`extract`) and asks
for confirmation before running; pass `--approve-consequential` in non-interactive contexts
(CI, AI agents), where the prompt is a hard error instead. Every other command runs without
asking.

Create the release file with `rlsbl release init`, which auto-detects project targets and scaffolds the TOML file.

First release: if the current version has never been tagged, `release` publishes it as-is (bump type is ignored).

Pre-release versions (e.g. `1.0.0-beta.1`) are supported.

## Scaffold

```
rlsbl scaffold                    # create or update CI/CD for all detected registries
rlsbl scaffold --target plain     # also cover a registry auto-detection cannot find
rlsbl scaffold --no-auto-commit   # skip auto-commit of scaffolded files
rlsbl scaffold --no-auto-tag      # skip the rlsbl GitHub topic tag on this run
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
| `.git/hooks/pre-push` | Captures push refs, runs `rlsbl check --tag prepush` |
| `.rlsbl/bases/` | Three-way merge bases for scaffold |

**Three-way merge:** Bases are stored at scaffold time. On re-run, user customizations and template updates merge via `git merge-file`. Conflicts get git-style conflict markers.

**User-owned files** (CHANGELOG.md, LICENSE, `.rlsbl/hooks/pre-checks.sh`, `.rlsbl/changes/unreleased.jsonl`) are never overwritten by a re-scaffold: there is no flag that makes scaffold clobber them.

**Customizing CI without conflicts:** Instead of editing `ci.yml` or `publish.yml` (which can produce merge conflicts on re-scaffold), put extra jobs in a separate workflow file scaffold never touches:

- `.github/workflows/ci-custom.yml` -- runs alongside `ci.yml`
- `.github/workflows/publish-custom.yml` -- runs alongside `publish.yml`

See [docs/ci-customization.md](docs/ci-customization.md) for an example.

**Runs config migrations** when `.rlsbl/config-schema.json` exists.

## Check system

:-: check-count

Checks are grouped by tag -- `--tag` runs one family, `--name` runs a single check, and `--all` runs everything, including the checks that carry no tag:

:-: table-check-tags

What each tag's checks actually verify, one row per check with its severity, is in [docs/checks.md](docs/checks.md), which also says which tags the release pipeline runs on its own.

```
rlsbl check --all              # run all checks
rlsbl check --tag changelog    # run checks by tag
rlsbl check --name lock        # run a single check
```

## Undo

```
rlsbl release undo                          # interactive: confirms once, then auto-pushes
rlsbl release undo --approve-consequential  # non-interactive: skips the confirmation
```

Reverts the last release:

1. Deletes the GitHub Release
2. Deletes the git tag (remote + local)
3. Reverts the version bump commit (if HEAD matches the tag)
4. Pushes the revert commit (the single confirmation covers the whole rollback)

On partial failure, prints a structured summary table with remediation commands for each failed step.

## Who writes which ref namespace

Each namespace has one routine writer -- the flow that puts refs there while
shipping -- and a named set of repair and retraction surfaces for correcting or
withdrawing what already shipped:

| Namespace | Routine writer |
|-----------|------------|
| `origin` branch heads | Releases. `rlsbl release run` pushes the untagged candidate and, after CI, the finalization commits; there is no dev-branch push path. |
| `origin` tags and their GitHub Releases | The release's tag step, repaired by `rlsbl release reconcile` when a rewrite or a partial release left them wrong -- both composing the Release through one module, so the notes and the `rlsbl-ci-sha` marker match either way. |
| A subtree mirror's `main` | The mirror reconciler's converge (`rlsbl monorepo mirror`, and the release's mirror step, which calls the same code). Force-with-lease is its routine write; a commit it cannot account for is a contract violation it refuses. |
| A subtree mirror's tags and their GitHub Releases | The mirror publication module, driven by the release's mirror step or by `rlsbl monorepo mirror` materializing a version the mirror is missing. A mirror's scaffold renders no publish workflow and every convergence sweeps one that arrived another way, so the mirror never releases itself. |
| Rewritten history on any of the above | `rlsbl release scrub`, the one sanctioned rewrite: it force-pushes, remaps the changelog hashes, re-points the tags and rewrites each tag's Release document in a single pass -- in place, never delete-then-create. |

The repair and retraction surfaces, in full: `rlsbl release undo` (deletes the
Release and the tag, reverts the version-bump commit and pushes the branch),
`rlsbl release reconcile` (re-pushes moved tags, writes their Release documents
in place and creates only the absent ones),
`rlsbl release scrub` (the rewrite), `rlsbl release edit` (re-syncs one
Release's notes), `rlsbl release deprecate` and `rlsbl release yank` (rewrite a
Release body and set its pre-release flag; `yank` also performs the registry's
removal), `rlsbl changelog amend` and `rlsbl changelog edit` (re-sync a released
version's Release notes), and `rlsbl monorepo rename-releasable` (pushes one
boundary alias tag). A write from anywhere else is not rlsbl's.

## Pre-push hook

The `.git/hooks/pre-push` hook captures push refs from git and runs `rlsbl check --tag prepush`, which enforces:

1. **Changelog coverage** -- every pushed commit must have a JSONL entry
2. **Gitignore guard** -- rlsbl-managed files must not be gitignored
3. **Manual push guard** -- hard error when pushing to a release branch outside `rlsbl release`
4. **Test suite** -- runs project tests (single-project) or affected project tests (monorepo)

The hook is namespace-aware: it enforces on `refs/heads/*` and exits 0 for `refs/tags/*` (release tags, pushed by rlsbl itself) and `refs/backups/*` (tool-owned backup slots). Release-internal pushes run `git push --no-verify` and never invoke the hook, so there is no environment-variable bypass to leak.

Old hooks that call `rlsbl pre-push-check` no longer work: the command was removed and now exits non-zero with an error, which blocks the push. Run `rlsbl scaffold` to install the current hook.

To reinstall, run `rlsbl scaffold` -- it writes the current hook (and upgrades any previously shipped version in place).

## Ecosystem tagging

`scaffold` and `release` add an `"rlsbl"` keyword to project manifests and set the `rlsbl` topic on the GitHub repository, making projects discoverable via `rlsbl discover`.

To disable:

| Method | Scope |
|--------|-------|
| `--no-auto-tag` flag | Single invocation |
| `{"tag": false}` in `.rlsbl/config.json` | This project |
| `{"tag": false}` in `~/.rlsbl/config.json` | All projects |

## Monorepo

Manage multi-package workspaces with `rlsbl monorepo`:

- `monorepo init` / `monorepo add` / `monorepo remove` -- workspace management
- `monorepo sync` -- synchronize CI workflows
- `monorepo graph` -- export dependency graph (DOT, text, or `--json` payload)
- `monorepo snapshot` -- committed JSON artifact of workspace state
- `monorepo impact` -- change analysis across the dependency graph
- `monorepo release run` -- batch release in topological order

Supports architectural layer rules via `[layers]` in `workspace.toml` for enforcing dependency direction.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RLSBL_VERSION` | -- | Set when running pre-release and post-release hooks; contains the version being released |
| `RLSBL_DIST_DIR` | -- | Set when running `custom_assets` build commands; points to the distribution directory for output files |
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
