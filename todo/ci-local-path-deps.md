# CI fails when uv.lock has local path dependencies

## Problem

Projects with `[tool.uv.sources]` editable path overrides (e.g., `fastware = { path = "../fastware", editable = true }`) generate a `uv.lock` where the resolved source is `source = { editable = "../fastware" }`. When CI runs `uv sync`, it tries to use the lockfile's resolution, which points to a path that doesn't exist on the runner.

`UV_NO_SOURCES=true` only ignores `[tool.uv.sources]` in pyproject.toml — it does NOT override the already-resolved paths in `uv.lock`. So `uv sync --locked` still fails.

## Affected projects

Any project with `[tool.uv.sources]` path dependencies. Currently: wesktop (depends on fastware, strictcli).

## Possible fixes

1. **CI workflow uses `--no-lock`**: Run `UV_NO_SOURCES=true uv sync --no-lock` to resolve fresh from PyPI, ignoring both sources and lockfile. Loses lockfile reproducibility in CI.

2. **CI workflow regenerates lockfile**: Run `UV_NO_SOURCES=true uv lock` then `uv sync --locked`. Produces a CI-compatible lockfile on the fly.

3. **Scaffold detects `[tool.uv.sources]`**: When generating CI workflows, if `[tool.uv.sources]` exists in `pyproject.toml`, add `UV_NO_SOURCES=true` env var and use `--no-lock` or regenerate the lockfile.

4. **uv native solution**: uv may add support for environment-conditional sources or lockfile overrides. Track upstream.

## Impact

wesktop v0.7.0 CI failed because of this. PyPI publish succeeded but CI tests never ran on the runner.
