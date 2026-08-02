# `workspace-unbuildable` false positive on polyglot monorepos with no root uv workspace

## Context

`rlsbl check` runs a `workspace-unbuildable` diagnostic that attempts to prove the workspace can actually build. For monorepos containing at least one `pypi` target, this check shells out to `uv sync --all-packages` **at the monorepo root**.

That command assumes a root uv workspace exists — i.e. a root `pyproject.toml` declaring `[tool.uv.workspace]` with member packages. It does not verify that assumption before running.

## Problem

On a polyglot monorepo, there is no root uv workspace and none is wanted:

- Multiple releasables of different ecosystems (e.g. a go releasable, a python releasable, a typescript releasable).
- A python project living standalone under its own directory with its own `pyproject.toml`.
- **No root `pyproject.toml`** — the repo root is not a uv workspace.

Every project builds and tests green individually (each in its own directory). But because at least one project has a `pypi` target, `workspace-unbuildable` unconditionally runs `uv sync --all-packages` at the root and fails with:

```
No pyproject.toml found
```

This is a **false positive**: the workspace is perfectly buildable; there simply is no root uv workspace to sync, and adding one is explicitly not wanted for this layout (the python packages are independent, not uv-workspace members).

### Reproduction shape

A polyglot monorepo with three releasables:

1. Create a monorepo with three releasables spanning different ecosystems (go + python + typescript), plus a standalone python project under its own directory with its own `pyproject.toml`. No root `pyproject.toml`.
2. Ensure each project builds and tests green in its own directory.
3. Run `rlsbl check --all`.
4. Observe `workspace-unbuildable` fails with "No pyproject.toml found" from `uv sync --all-packages` at the root, despite every project being individually buildable.

## Scope / impact

Currently the check is tagged only `workspace`, so it does **not** gate releases (release preflight does not run it). The impact is therefore limited to `rlsbl check --all` **honesty**: the diagnostic reports the workspace as unbuildable when it is not, eroding trust in `check --all` output and creating noise that can mask real failures. This is not a release-blocking bug — the fix is about correctness of the diagnostic, not unblocking releases.

## Root cause

The check couples "has a pypi target somewhere" to "there is a root uv workspace to `uv sync --all-packages`." Those are independent. A pypi target can be a standalone package in its own directory with no relationship to a root workspace.

## Affected files

- The `workspace-unbuildable` check implementation (the code that decides to run `uv sync --all-packages` at the monorepo root when any project has a `pypi` target). Locate it under the checks/diagnostics module (search for `--all-packages` and the `workspace-unbuildable` check name/registration).

## Solution options

### Option A — skip the check when no root `pyproject.toml` exists

Before running `uv sync --all-packages`, check for a root `pyproject.toml` (ideally one declaring `[tool.uv.workspace]`). If absent, skip the root-level build check (the repo is not a uv workspace).

- Pros: minimal, obviously correct — you cannot `uv sync` a workspace that does not exist; removes the false positive with almost no risk.
- Cons: skipping means the pypi target(s) get no build verification from this check at all (unless combined with Option B). Could mask a genuinely unbuildable standalone pypi package.

### Option B — run `uv sync` per pypi-target project directory instead of at the root

Instead of one root `uv sync --all-packages`, iterate the projects that have a `pypi` target and run `uv sync` in each project's own directory.

- Pros: actually verifies each pypi package builds, in the location where it is defined; correct for polyglot repos with independent python packages; no root workspace assumption.
- Cons: more invocations (slower); needs to correctly resolve each pypi project's directory; for repos that DO use a real root uv workspace, per-directory sync may not match the workspace-resolved environment (so detection of "is there a root workspace" is still needed to choose the strategy).

### Option C — config key declaring workspace layout

Add a config key (e.g. in the workspace/monorepo config) describing whether a root uv workspace exists and, if so, where — driving how `workspace-unbuildable` runs.

- Pros: explicit, no guessing; matches the ecosystem's "explicit mode selection over silent detection" preference; supports odd layouts.
- Cons: more configuration surface; requires every existing polyglot monorepo to set the key; heavier than the problem strictly needs.

### Recommended combination

Detect whether a root uv workspace exists (presence of root `pyproject.toml` with `[tool.uv.workspace]`):

- If it exists → keep the current `uv sync --all-packages` at the root.
- If it does not → run `uv sync` per pypi-target project directory (Option B behavior) rather than failing.

This is Option A's detection combined with Option B's fallback strategy, chosen explicitly rather than silently — no root workspace is a real layout, not an error. Option C can layer on later if auto-detection proves insufficient for some layout.

## Effort estimate

Small. Add a root-workspace detection helper, branch the check strategy on it, and run per-project `uv sync` for pypi targets when there is no root workspace. Add a test fixture representing a polyglot monorepo with no root `pyproject.toml` and assert `workspace-unbuildable` passes (or is correctly skipped/handled) when every project is individually buildable. Estimate ~0.5 day including the fixture and test.
