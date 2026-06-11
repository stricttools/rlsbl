# Add --no-sources to CI scaffold template

## Problem

The CI scaffold template uses `uv pip install` (or `uv sync`) without `--no-sources`. When a project has `[tool.uv.sources]` with local editable paths in pyproject.toml, CI fails because those paths don't exist on GitHub Actions runners.

## Solution

Add `--no-sources` to the `uv pip install` or `uv sync` command in the CI template. This tells uv to ignore local source overrides and resolve from PyPI instead.

The flag exists specifically for this use case (local dev uses sources, CI ignores them).

## Affected template

The pypi CI template at `rlsbl/templates/pypi/ci.yml.tpl` — the install/sync step needs `--no-sources`.

## Scope

11 projects under ~/Projects/ have `[tool.uv.sources]` and are affected. Fixing the template propagates to all future scaffolds. Existing projects need individual `ci.yml` fixes (or a re-scaffold).

## Upstream: uv feature request

- Issue: https://github.com/astral-sh/uv/issues/19701
- What was requested: `--no-path-sources` flag to ignore path/editable entries while preserving workspace sources
- Status: filed, waiting on uv team
- This todo is blocked until uv ships the feature (or an alternative selective source mechanism)
