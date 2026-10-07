+++
description = "Local development with rlsbl: dev install for the go, npm, and pypi targets, editable overlays of sibling checkouts with dev sync and their drift detection through dev status and dev-overlay-drift, watching CI with rlsbl watch, and the pre-push hook."
+++

# Development workflow

## Installing a project locally

`rlsbl dev install --target global|venv` installs the project the way its targets install, running each target's own command from the target's directory:

| Target | `--target global` | `--target venv` | `--uninstall` |
| --- | --- | --- | --- |
| go | `go install` of each main package the member's go pipeline names in `install_paths` | nothing | nothing |
| npm | `npm link` | `npm install` | `npm unlink` |
| pypi | `uv tool install -e .` | `uv sync --all-packages` | `uv tool uninstall <package name>` |

A target with nothing to run in the chosen mode is skipped with the reason. Every command is planned before any runs, and the run is refused before anything is installed when a program is not on `PATH`, when a Go module has main packages but no `install_paths` (or two go pipelines declare different ones), when a member has no targets, or when an uninstall's manifest names no package. A command that fails does not stop the others, and the run exits 1 once all were tried.

In a workspace, `--all` installs every member that is not dev-only, `--include <names>` only the members it names (dev-only ones included), and `--exclude <names>` every member that is not dev-only but those; a name the workspace does not declare is refused, and so is `--all` with `--include`. A standalone repository takes none of the three.

## Local editable overlays (`rlsbl dev sync`)

`rlsbl dev sync` overlays editable checkouts of sibling projects onto a member's locked Python environment: the way to develop against a library you are changing in step, without committing a `[tool.uv.sources]` path entry. A committed path source that resolves outside the repository is refused by the `cross-repo-path-sources` check and by every release, which keeps each committed lockfile resolvable from the registry and lets CI run `uv sync --locked`.

### The overlay file

Overlays are declared in `dev-sources.toml.local-only` in the member's directory (the `*.local-only` suffix keeps it out of git), one table per checkout:

```toml
[[overlay]]
package = "strictcli"          # the distribution name
path = "../strictcli/python"   # absolute, or relative to the member's directory
```

The file is read strictly. It is refused for an unknown key, a checkout that does not exist or whose `[project].name` differs from `package`, a package named twice, a package a workspace member builds, and a checkout inside this repository.

### What dev sync does

1. Refuses unless `UV_NO_SYNC=1` is in the environment: without it, any bare `uv run` syncs the environment back to the locked wheels and wipes the overlays. Set it once in the shell profile or the project's `.envrc`.
2. Runs one `uv sync --inexact` leaving out every overlaid package.
3. Runs `uv pip install -e <checkout>` per overlay, which also picks up the checkout's new dependencies.
4. Records the overlays in `dev-overlays-state.toml.local-only`.

Every uv command runs with `VIRTUAL_ENV` set to the member's environment (the workspace root's `.venv` for a uv workspace member, or `UV_PROJECT_ENVIRONMENT` where it is set), so the sync and the installs reach one environment. A workspace's root member is refused: run it from a member's directory. A bare `uv sync` later reverts the overlays; run `rlsbl dev sync` again to restore them.

### Detecting a wiped overlay

`rlsbl dev status` compares each recorded overlay with what the member's environment holds: editable at the recorded checkout, `WIPED` back to a registry wheel, or `MISSING`. Whether an install is editable is read from its dist-info's `direct_url.json`. It exits 1 when any overlay drifted, naming `rlsbl dev sync`, so a script or a pre-run guard sees the wipe; it exits 0 when every overlay is intact or none is recorded. The `dev-overlay-drift` check asks the same question in every release's preflight.

The declaration and the record together say which mode the environment is in: neither present is registry mode (CI, and a machine without overlays), both present and agreeing is overlay mode, and any disagreement is an error naming both files. In overlay mode, the `test-suite` checks sync with every overlaid package left out and run the suite with `uv run --no-sync`, so the overlays are tested, not wiped. A release refuses an overlay whose local version is ahead of the package's latest PyPI release: the work was developed against unreleased code, and the dependency is released first.

## Watching CI

`rlsbl watch [<sha>]` watches the GitHub Actions runs of a commit (`HEAD` when none is named), or the runs `--run-id` names, until each concludes:

1. When the commit is a release commit of a releasable the record holds, every release-triggered workflow of the tagged tree must show a run for its tag, or the command fails naming what it found. GitHub reports no error when a Release starts nothing. A Release older than the repository's run retention has had its runs deleted, which is reported instead.
2. A commit on which no run appears within two minutes fails, naming the command to run again.
3. Every run is watched; a run `gh` stops watching without a pass counts as failed only when the run's own state says it completed, and is watched again otherwise.
4. A failed run is classified from its failing jobs' logs. A deterministic failure (tests, compilation, configuration, workflow syntax, a missing secret) is reported with the command that runs its failed jobs by hand; any other failure is run again once, in place (only the failed jobs for an infrastructure failure, where the run never executed).
5. Runs that start after the first listing are watched too.

It exits 0 when every run passed and 1 otherwise. `rlsbl release run --watch`, `rlsbl release resume --watch`, and `rlsbl release retry --watch` watch the same way in-process; `--no-watch` prints the `rlsbl watch` command instead.

## The pre-push hook

`rlsbl scaffold` installs `.git/hooks/pre-push`, which runs `rlsbl failing-checks --hook pre-push` for any push to a branch, with git's ref lines in `RLSBL_PUSH_STDIN`; a push of only tags or `refs/backups/*` exits at once. `[hooks.pre-push]` in rlsbl's `checks.toml` selects the `prepush` tag, and `failing-checks` blocks on error-level failures only:

| Check | What it holds |
| --- | --- |
| `prepush-changelog-coverage` | Every pushed commit has an entry in the changelog of each releasable it touches; commits with the `Autogenerated: true` trailer and commits changing only rlsbl's records are exempt. |
| `prepush-gitignore-guard` | No changelog record is gitignored. |
| `prepush-manual-warning` | No push to a release branch (the declared `release_branches`) outside a release; a release pushes with `--no-verify`, so a push the hook sees there is a manual one. |
| `scaffold-conflicts` | No managed file carries conflict markers. |
| `test-suite` | The project's tests pass, with the member's dev overlays kept; a workspace's root member is skipped. |
| `test-suite-workspace` | The tests of every member the push changes pass; dev-only members are skipped, and the members of one uv workspace are synced once. |

The test checks depend on `prepush-changelog-coverage`, so a push missing an entry fails before any test runs. There is no environment-variable bypass. `rlsbl check --hook pre-push` prints the full report of the same selection, and outside a push the push-specific checks skip.

A hook installed by an earlier rlsbl (one calling the pre-push-check command earlier versions had, say) is replaced by the next `rlsbl scaffold`, which recognizes every hook rlsbl shipped.
