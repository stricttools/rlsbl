---
description: "Local development: editable installs per target, sibling overlays via dev sync, overlay drift detection, CI watching with retry, and pre-push enforcement."
---

# Development workflow

## Editable installs

`rlsbl dev install` installs a project locally for development using the target's native editable install mechanism. It detects project targets, calls each target's `dev_install_command()` to get the install spec, and runs it.

### Per-target install commands

Seven of rlsbl's release targets implement an editable install command; every other target skips with an explanatory message rather than failing the run. Each target reports a global command, a venv command, or both, so `rlsbl dev install` never has to guess which ecosystem tool to invoke. Targets that cannot support a mode at all report it as unsupported, and the command says so instead of silently doing nothing.

| Target | Global install command | Venv install command |
| --- | --- | --- |
| pypi | `uv tool install -e .` | `uv sync --all-packages` |
| npm | `npm link` | `npm install` |
| go | `go install <install_paths>` (declared on the go pipeline in `.rlsbl/config.json`) | (not supported) |
| deno | `deno install` | `deno cache .` |
| hex | `mix deps.get` | `mix deps.get` |
| swift | `swift build` | (not supported) |
| zig | `zig build install` | (not supported) |

### Modes

| `--target` value | Behavior |
| --- | --- |
| `global` | System-wide install via the target's global command |
| `venv` | Project-local environment install (supported by pypi, npm, deno, and hex) |

`--target` is required and has no default: `rlsbl dev install` without it is a parse-time error naming the flag. The mode is always stated, never assumed.

### Uninstall

`rlsbl dev install --target global --uninstall` reverses a previous install by invoking each target's native removal command. The uninstall mechanism is stateless -- rlsbl does not track which packages were installed, so it relies entirely on the ecosystem's own uninstall tooling to determine what to remove:

| Target | Uninstall command | Notes |
| --- | --- | --- |
| pypi | `uv tool uninstall {name}` | Resolves package name from `pyproject.toml` |
| npm | `npm unlink` | Removes the global symlink |
| deno | `deno uninstall {name}` | Removes the installed script |
| go, hex, swift, zig | (skipped) | No uninstall template; prints a message |

### Monorepo mode

In a monorepo workspace with multiple independently-versioned projects, `rlsbl dev install` requires an explicit filter flag to specify which projects to install. Without a filter, the command errors with guidance rather than installing everything by default, preventing accidental system-wide installation of dozens of packages:

| Flag | Behavior |
| --- | --- |
| `--all` | Install every project in the workspace |
| `--include <names>` | Comma-separated project names to include |
| `--exclude <names>` | Comma-separated project names to exclude |

Without a filter flag, the command errors with guidance. Install and uninstall operations apply recursively to matching projects.

## Local editable overlays (`rlsbl dev sync`)

`rlsbl dev sync` overlays local editable checkouts of sibling projects onto the current project's locked environment -- the supported way to develop against a sibling checkout (e.g. a library you are changing in lockstep) without committing machine-local `[tool.uv.sources]` path dependencies, which poison `uv.lock` with machine-specific paths and break CI.

Committing such path sources is not just discouraged -- it is banned outright. The `cross-repo-path-sources` check (project tag, also enforced unconditionally by `rlsbl release run`) hard-errors when a committed `pyproject.toml` declares a `[tool.uv.sources]` path entry that resolves outside the repository, whether absolute (`/home/user/other-repo`) or relative (`../sibling`, resolved against the pyproject's directory, matching uv's rule). In-repo paths and `workspace = true` sources stay legal. The ban is what keeps every committed lockfile registry-pure, which in turn lets scaffolded CI run `uv sync --locked` unconditionally.

The overlay file also feeds the release-time **version-skew guard**: `rlsbl release run` reads `dev-sources.toml.local-only` and hard-errors when any overlaid checkout's local version is ahead of its latest registry release ("release the dependency first") -- releasing code developed and tested against unreleased dependency features would ship something the registry cannot satisfy. See the [release workflow](release-workflow.md) for details.

### Why a wrapper is required

A wrapper command is required because, as of uv 0.9.17, no single native uv mechanism can install an editable sibling checkout and prevent subsequent sync operations from reverting it back to the locked registry wheel. Each partial solution has a gap that the wrapper fills by orchestrating multiple uv invocations together:

- `uv pip install -e ../x` alone is wiped by the next `uv sync`: exact sync reinstalls the locked registry wheel even at equal versions.
- `uv sync --inexact --no-install-package <name>` preserves a pre-existing editable install (even under version conflict, and with `--frozen`), but neither flag has an environment-variable equivalent.
- A bare `uv run` auto-syncs (and wipes overlays) unless `UV_NO_SYNC=1` is set.
- `[sources]` in `uv.toml` is rejected by uv, and the `UV_SOURCES` environment variable is silently ignored (unshipped proposal).

### The overlay file

Overlays are declared in `dev-sources.toml.local-only` at the project root. The `*.local-only` suffix is ignored by the scaffold gitignore fleet-wide, so the file never reaches git. One `[[overlay]]` block per checkout:

```toml
[[overlay]]
package = "strictcli"          # distribution name, as uv knows it
path = "../strictcli/python"   # absolute, or relative to the project root
```

Every problem is a hard error, never a silent no-op: missing file (the error shows the format above), invalid TOML, unknown keys, missing `package` or `path`, nonexistent path, path without a `pyproject.toml`, and a `package` that does not match the checkout's `[project].name` (PEP 503-normalized) -- a mismatch would make the sync exclusion miss, letting the next sync silently wipe the overlay.

### Behavior

1. Hard-errors unless `UV_NO_SYNC=1` is set in the environment (see below).
2. Runs a single `uv sync --inexact` with `--no-install-package <pkg>` for every overlay entry.
3. Runs `uv pip install -e <path>` per entry. Re-installing on every run is deliberate: it picks up new transitive dependencies of the overlaid checkouts.
4. Prints exactly what was overlaid: package, version (from the checkout's `pyproject.toml`), and resolved path.

`VIRTUAL_ENV` is stripped from both subprocess invocations so `uv sync` and `uv pip` deterministically target the same project environment (a leaked active venv would otherwise split the two steps across environments). The command is idempotent. In a monorepo, run it from within a sub-project; invoking it at the workspace root is a hard error.

### How an overlay is detected (`rlsbl dev status`, `dev-overlay-drift`)

`rlsbl dev status` and the `dev-overlay-drift` check compare the sentinel against the environment uv actually manages for the project. Whether an install is editable is read from the installed distribution's PEP 610 `direct_url.json` (`dir_info.editable`, plus the `file://` checkout the URL names), through `importlib.metadata`. Nothing in the file layout is consulted: an editable install writes a dist-info and a `.pth` import hook whose name and content vary by build backend, and never creates a package directory in `site-packages`, so a test that inspected the directory layout would report a healthy overlay as missing.

The environment is resolved the way uv resolves it, not as "a `.venv` beside the project":

- A **uv workspace member** has no environment of its own -- `uv sync` and `uv pip install` from inside the member both target the workspace root's `.venv`, and that is where the detector looks.
- `UV_PROJECT_ENVIRONMENT` relocates it; a relative value is resolved against the workspace root, exactly as uv does.

A package the environment does not hold at all reports as missing and names the environment directory that was inspected.

### Overlay mode is what other commands read

The declaration and the sentinel together say which mode the environment is in: neither file present is registry mode (CI, and every machine with no overlays), both present and agreeing is overlay mode, and any disagreement -- a declaration that was never synced, a sentinel with no declaration, different package sets, or a package synced from a path other than the one declared -- is a hard error naming both files. Never a silent choice of one mode: picking registry mode would wipe the overlays, and picking overlay mode would test against a checkout nobody declared.

Every command that syncs an environment reads this. In overlay mode the built-in `test-suite` and `test-suite-workspace` checks (which the release preflight and the pre-push hook run) sync with `--inexact --no-install-package <pkg>` per overlay, and run the suite with `uv run --no-sync` -- the same shape as `rlsbl dev sync` and the sandboxed test runner. A bare `uv sync` in that position would reinstall the locked registry wheels over the checkouts, so the suite would silently test released code and the `dev-overlay-drift` check would then fail on the wipe the check itself caused. In a workspace, the members share one environment, so the sync at the workspace root excludes every member's overlaid packages; two members overlaying one package from different checkouts is a hard error.

### The UV_NO_SYNC=1 gate

Without `UV_NO_SYNC=1`, any bare `uv run` silently reinstalls the locked registry wheels over the editable overlays just created by the sync command. The result is that overlays would be silently reverted on every uv run invocation, undoing the entire dev sync setup. `rlsbl dev sync` therefore refuses to run until `UV_NO_SYNC=1` is set permanently in the shell profile or direnv configuration:

```bash
# shell profile (~/.bashrc / ~/.zshrc) or the project's .envrc (direnv)
export UV_NO_SYNC=1
```

A bare `uv sync` still reverts overlays -- harmlessly: re-run `rlsbl dev sync` to restore them.

## Watch and CI monitoring

`rlsbl watch [<sha>]` polls GitHub Actions for a commit's CI runs and reports pass/fail results in real time, with automatic retry on transient failures. It discovers runs by commit SHA (defaulting to HEAD), watches them concurrently, and sends desktop notifications on completion with links to the relevant GitHub page.

### Behavior

1. **Discovery** -- polls `gh run list --commit <sha>` until at least one run appears (30 attempts, 4 seconds apart: about two minutes)
2. **Parallel watching** -- all discovered runs are watched concurrently via `gh run watch`
3. **Classified auto-retry** -- when a workflow fails, the failing step's log tail is fetched and classified. A DETERMINISTIC failure (a test failure, a compile or config error, a workflow syntax error, a missing-secret or auth denial) will fail identically on a rerun and is never retried. Anything else is treated as a transient flake and retried exactly once, in place, via `gh run rerun <failed_run_id>` -- a full rerun of the same run id, not a `gh workflow run` dispatch (a dispatch would create a run the publish gate cannot match). The rerun is then watched to completion.
4. **Late-starting workflow detection** -- after initial runs complete, polls once more for workflows that started late (e.g., publish/deploy workflows triggered by a GitHub Release created during CI). Late runs are watched with the same parallel/retry logic.
5. **Workflow audit** -- prints a summary table of all workflows that ran. Warns if a publish workflow exists on disk but did not trigger for this commit.
6. **Desktop notifications** -- sends a notification on completion:
   - On failure: opens the Actions page for the failed run
   - On success: opens the GitHub Release page (if a tag exists for this commit)

### Flags

| Flag | Description |
| --- | --- |
| `--target <name>` | Registry whose CI workflow to watch (auto-detected if omitted) |
| `--run-id <id>` | Watch specific run IDs instead of discovering by commit (repeatable) |

## Pre-push hook

The `.git/hooks/pre-push` hook captures git's stdin into the `RLSBL_PUSH_STDIN` environment variable and runs `rlsbl check --tag prepush`, enforcing changelog coverage, gitignore safety, and test suite execution before any commits reach the remote. This hook is installed by `rlsbl scaffold` and uses the V5 template format. It runs all prepush-tagged checks in dependency order:

1. **`prepush-changelog-coverage`** (error) -- verifies every pushed commit has a JSONL changelog entry. Commits that only touch `.rlsbl/changes/` or `CHANGELOG.md`, and commits with an `Autogenerated: true` trailer, are automatically exempted.
2. **`prepush-gitignore-guard`** (error) -- blocks the push if rlsbl-managed files (e.g., `.rlsbl/changes/unreleased.jsonl`, `CHANGELOG.md`) are gitignored.
3. **`prepush-manual-warning`** (error) -- blocks a push that targets a release branch (configured via `release_branches` in `.rlsbl/config.json`). Release-internal pushes run `git push --no-verify` and never reach the hook, so any push that does reach this check is by construction a manual one. There is no environment-variable bypass.
4. **`test-suite`** (error) -- runs the project's test suite (`pytest` / `go test` / `npm test`). Depends on `prepush-changelog-coverage` -- if changelog coverage fails, the test suite is skipped (fast checks fail first).

### Key differences from the old system

- **Version-tag pushes are no longer exempt.** All checks always run, regardless of whether the push contains a version bump commit.
- **Fast-fail ordering.** Dependency ordering ensures cheap checks run before expensive ones. If `prepush-changelog-coverage` fails, `test-suite` is skipped entirely.
- **Unified check system.** The pre-push hook uses the same `rlsbl check` infrastructure as release validation, with consistent reporting and severity handling.

### Monorepo behavior

In monorepos, the pre-push hook runs from the repo root. The `test-suite` check hard-errors at workspace root because it needs a specific project directory. The `test-suite-workspace` check handles this: it automatically detects affected projects from push refs, runs tests for each, and skips dev nodes (`dev_only = true` with `releasable = false`). If changelog coverage fails, `test-suite-workspace` is skipped (it depends on `prepush-changelog-coverage`).

### Standalone usage

`rlsbl check --tag prepush` can be run outside of a git push context. Push-specific checks (`prepush-changelog-coverage`, `prepush-gitignore-guard`, `prepush-manual-warning`) skip gracefully when `RLSBL_PUSH_STDIN` is not set. `test-suite` always runs regardless of push context.

### Removed command

The old `rlsbl pre-push-check` command was removed. It is now a stub that prints an error and exits non-zero, so a hook still calling it blocks every push. Run `rlsbl scaffold` to install the V5 hook template, which calls `rlsbl check --tag prepush` instead.

## Examples

### Setting up local development for a Python project

```bash
cd ~/Projects/mylib

# Install the project locally for development (editable install)
rlsbl dev install --target global
#   Installing mylib via: uv tool install -e .
#   Installed mylib 0.5.2

# Verify it works
mylib --version
#   0.5.2
```

### Developing against a local sibling checkout

When making coordinated changes to a library and its consumer simultaneously, use `rlsbl dev sync` to overlay a local editable checkout of the library onto the consumer's locked virtual environment. This avoids publishing intermediate versions and lets you test changes across project boundaries immediately. The overlay mechanism uses `uv pip install -e` under the hood and requires `UV_NO_SYNC=1` in your environment to prevent bare `uv run` from reverting the overlay back to a registry wheel:

```bash
cd ~/Projects/myapp

# 1. Set UV_NO_SYNC=1 in your shell profile (one-time setup)
echo 'export UV_NO_SYNC=1' >> ~/.bashrc
source ~/.bashrc

# 2. Create the overlay file
cat > dev-sources.toml.local-only << 'EOF'
[[overlay]]
package = "strictcli"
path = "../strictcli/python"
EOF

# 3. Run dev sync to overlay the local checkout
rlsbl dev sync
#   uv sync --inexact --no-install-package strictcli ... OK
#   uv pip install -e ../strictcli/python ... OK
#   Overlaid: strictcli 0.8.0 from ../strictcli/python

# Now edits to ../strictcli/python are immediately visible in myapp's environment
```

### Checking overlay status

```bash
rlsbl dev status
#   strictcli  0.8.0  ../strictcli/python  editable (intact)
```

### Installing all projects in a monorepo

```bash
cd ~/Projects/my-monorepo

# Install all workspace projects
rlsbl dev install --target global --all
#   Installing mylib via: uv tool install -e packages/mylib
#   Installing cli via: npm link (packages/cli)
#   Skipping tests (dev-only)

# Or install specific projects
rlsbl dev install --target global --include mylib,cli

# Uninstall when done
rlsbl dev install --target global --uninstall --all
```

### Watching CI after a push

```bash
# Watch CI for the latest commit
rlsbl watch
#   Discovering runs for abc1234 ...
#   Watching: CI (abc1234) ... running
#   CI (abc1234) ... passed

# Watch CI for a specific commit
rlsbl watch e4f5g6h
#   Discovering runs for e4f5g6h ...
#   Watching: CI (e4f5g6h) ... running
#   CI (e4f5g6h) ... failed
#   Auto-retrying CI ...
#   Watching: CI (e4f5g6h) [retry] ... passed
```
