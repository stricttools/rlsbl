# test-suite-workspace runs uv sync per sub-project instead of at workspace root

## Problem

The `test-suite-workspace` pre-push check (in `testing.py`) runs `uv sync --all-packages` with `cwd=<sub-project-directory>` for each affected project. In a uv workspace, this should sync at the workspace root so all members share the workspace venv. Instead, each sub-project gets its own isolated sync, which fails because sibling packages aren't resolved.

## Root cause

`run_project_tests()` in `testing.py:49` uses `project_dir` as the cwd for both `uv sync` and `uv run pytest`. The correct behavior for uv workspaces: run `uv sync --all-packages` once at the workspace root before testing, then run `uv run pytest <sub-project>/tests/` per affected project from the workspace root.

## Impact

The pre-push hook fails for every monorepo that uses uv workspace with cross-project dependencies. The initial push is blocked.

## Suggested fix

In `workspace.py` (or `testing.py`), when the project is a monorepo:
1. Run `uv sync --all-packages` once at `ctx.workspace_root` (not per sub-project)
2. Run `uv run pytest <project_path>/tests/` per affected project, from `ctx.workspace_root`

This ensures all workspace members are installed and pytest can resolve imports.
