# Scaffold generates broken CI import check for workspace roots

## Problem

`rlsbl scaffold` at a monorepo workspace root generates a `.github/workflows/ci.yml` with an import check like `uv run python -c "import www_workspace"`. The import name is derived from the root `pyproject.toml` `name` field (e.g., `www-workspace` → `import www_workspace`).

Workspace roots are not importable Python packages — they have no source directory matching the import name. The generated CI workflow always fails with `ModuleNotFoundError`.

## How it happens

The CI template is the same for per-package and workspace root scaffolds. Per-package, the import check is correct (e.g., `import www_core` for the `core` package). At the workspace root, the template applies the same pattern but there is no corresponding Python module.

## Impact

Every push to main triggers a failing CI run. The ci-router handles per-package CI separately, so the root workflow is redundant — but its failure shows as a red badge on the repo.

## Proposed fix

When scaffolding at a monorepo workspace root (detected by the presence of `.rlsbl-monorepo/` or `[tool.uv.workspace]` in pyproject.toml), either:
1. Skip the import check step entirely (replace with `uv sync` only, or `uv run pytest`)
2. Don't generate a root-level `ci.yml` at all (the ci-router handles per-package CI)

## Affected files

- `rlsbl/commands/init_cmd.py` — CI template generation
- `rlsbl/templates/` — CI workflow templates
