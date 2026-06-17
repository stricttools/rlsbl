# Built-in test runner uses bare `pytest` instead of `uv run pytest`

## Problem

The built-in test runner in `rlsbl release run` (step 9) uses the system `pytest` binary instead of `uv run pytest` for Python (pypi) projects. The rlsbl documentation says it runs `uv run pytest`, but the actual implementation runs bare `pytest`.

This causes failures when the project's dependencies (e.g., psycopg, sentence-transformers) are only installed in the project's venv, not system-wide. The venv is managed by `uv` and dependencies are only available via `uv run`.

## Current workaround

Projects work around this by customizing `pre-checks.sh` or `pre-release.sh` to run `uv run python -m pytest` explicitly, which causes the built-in test step to be skipped (since tests already ran in the hook).

## Expected behavior

For Python (pypi) target projects, the built-in test step should run `uv run pytest` (or `uv run python -m pytest`) instead of bare `pytest`. This ensures the project's venv is activated and all dependencies are available.

## Impact

Every Python project using rlsbl that has venv-only dependencies must add a workaround hook. This is a common case since `uv`-managed projects deliberately keep dependencies out of the system Python.
