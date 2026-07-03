---
description: "Local development workflow: editable installs across 8 targets, local editable overlays via dev sync, CI watching with auto-retry, and pre-push hook enforcement."
---

# Development workflow

## Editable installs

`rlsbl dev install` installs a project locally for development using the target's native editable install mechanism. It detects project targets, calls each target's `dev_install_command()` to get the install spec, and runs it.

### Per-target install commands

| Target | Global install command | Venv install command |
| --- | --- | --- |
| pypi | `uv tool install -e .` | `uv sync` |
| npm | `npm link` | `npm install` |
| go | `go install <install_paths>` (declared on the go pipeline in `.rlsbl/config.json`) | (not supported) |
| cargo | `cargo install --path .` | (not supported) |
| deno | `deno install` | (not supported) |
| zig | (not yet supported) | (not yet supported) |
| swift | `swift build` | (not supported) |
| hex | `mix escript.install` | (not supported) |

### Modes

| Flag | Behavior |
| --- | --- |
| `--global` (default) | System-wide install via the target's global command |
| `--venv` | Project-local environment install (only supported by pypi and npm) |

When neither `--global` nor `--venv` is passed, `--global` is the default. The two flags are mutually exclusive.

### Uninstall

`rlsbl dev install --uninstall` reverses a previous install by invoking each target's native removal command. The uninstall mechanism is stateless -- rlsbl does not track which packages were installed, so it relies entirely on the ecosystem's own uninstall tooling to determine what to remove:

| Target | Uninstall command | Notes |
| --- | --- | --- |
| pypi | `uv tool uninstall {name}` | Resolves package name from `pyproject.toml` |
| npm | `npm unlink` | Removes the global symlink |
| go | (skipped) | No clean uninstall mechanism; prints a message |
| cargo | (skipped) | No clean uninstall mechanism; prints a message |

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

Verified against uv 0.9.17, no native uv mechanism suffices:

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

### The UV_NO_SYNC=1 gate

Without `UV_NO_SYNC=1`, any bare `uv run` silently reinstalls the locked registry wheels over the overlays just created. `rlsbl dev sync` therefore refuses to run until it is set permanently:

```bash
# shell profile (~/.bashrc / ~/.zshrc) or the project's .envrc (direnv)
export UV_NO_SYNC=1
```

A bare `uv sync` still reverts overlays -- harmlessly: re-run `rlsbl dev sync` to restore them.

## Watch and CI monitoring

`rlsbl watch [<sha>]` polls GitHub Actions for a commit's CI runs and reports pass/fail results in real time, with automatic retry on transient failures. It discovers runs by commit SHA (defaulting to HEAD), watches them concurrently, and sends desktop notifications on completion with links to the relevant GitHub page.

### Behavior

1. **Discovery** -- polls `gh run list --commit <sha>` until at least one run appears (up to 30 seconds)
2. **Parallel watching** -- all discovered runs are watched concurrently via `gh run watch`
3. **Auto-retry** -- if a workflow fails, it is automatically re-triggered once via `gh workflow run` (unconditional -- does not distinguish failure types). The retry run is then watched to completion.
4. **Late-starting workflow detection** -- after initial runs complete, polls once more for workflows that started late (e.g., publish/deploy workflows triggered by a GitHub Release created during CI). Late runs are watched with the same parallel/retry logic.
5. **Workflow audit** -- prints a summary table of all workflows that ran. Warns if a publish workflow exists on disk but did not trigger for this commit.
6. **Desktop notifications** -- sends a notification on completion:
   - On failure: opens the Actions page for the failed run
   - On success: opens the GitHub Release page (if a tag exists for this commit)

### Flags

| Flag | Description |
| --- | --- |
| `--run-id <id>` | Watch specific run IDs instead of discovering by commit (repeatable) |

## Pre-push hook

The `.git/hooks/pre-push` hook captures git's stdin into the `RLSBL_PUSH_STDIN` environment variable and runs `rlsbl check --tag prepush`, enforcing changelog coverage, gitignore safety, and test suite execution before any commits reach the remote. This hook is installed by `rlsbl scaffold` and uses the V5 template format. It runs all prepush-tagged checks in dependency order:

1. **`prepush-changelog-coverage`** (error) -- verifies every pushed commit has a JSONL changelog entry. Commits that only touch `.rlsbl/changes/` or `CHANGELOG.md`, and commits with an `Autogenerated: true` trailer, are automatically exempted.
2. **`prepush-gitignore-guard`** (error) -- blocks the push if rlsbl-managed files (e.g., `.rlsbl/changes/unreleased.jsonl`, `CHANGELOG.md`) are gitignored.
3. **`prepush-manual-warning`** (warn) -- warns when a push targets a release branch (configured via `release_branches` in `.rlsbl/config.json`) and did not originate from `rlsbl release run` or `rlsbl release undo`. Non-blocking.
4. **`test-suite`** (error) -- runs the project's test suite (`pytest` / `go test` / `npm test`). Depends on `prepush-changelog-coverage` -- if changelog coverage fails, the test suite is skipped (fast checks fail first).

### Key differences from the old system

- **Version-tag pushes are no longer exempt.** All checks always run, regardless of whether the push contains a version bump commit.
- **Fast-fail ordering.** Dependency ordering ensures cheap checks run before expensive ones. If `prepush-changelog-coverage` fails, `test-suite` is skipped entirely.
- **Unified check system.** The pre-push hook uses the same `rlsbl check` infrastructure as release validation, with consistent reporting and severity handling.

### Monorepo behavior

In monorepos, the pre-push hook runs from the repo root. The `test-suite` check hard-errors at workspace root because it needs a specific project directory. The `test-suite-workspace` check handles this: it automatically detects affected projects from push refs, runs tests for each, and skips `dev_node` projects. If changelog coverage fails, `test-suite-workspace` is skipped (it depends on `prepush-changelog-coverage`).

### Standalone usage

`rlsbl check --tag prepush` can be run outside of a git push context. Push-specific checks (`prepush-changelog-coverage`, `prepush-gitignore-guard`, `prepush-manual-warning`) skip gracefully when `RLSBL_PUSH_STDIN` is not set. `test-suite` always runs regardless of push context.

### Deprecated command

The old `rlsbl pre-push-check` command is deprecated. Update your hooks to the current version by running `rlsbl scaffold`, which installs the V5 hook template that calls `rlsbl check --tag prepush` instead.
