# `rlsbl dev install` — local editable install per target

## Problem

After scaffolding a project, the developer needs to install it locally for testing. The command differs by target:
- pypi: `uv tool install -e .` or `pip install -e .`
- npm: `npm link`
- go: `go install ./...`

There's no single command that does the right thing for the detected target.

## Proposed behavior

`rlsbl dev install` detects the target from `.rlsbl/config.json` and runs the appropriate local install:

| Target | Command | Result |
|--------|---------|--------|
| pypi | `uv tool install -e .` (or `pip install -e .` if uv unavailable) | CLI available globally, changes reflected immediately |
| npm | `npm link` in the package directory | CLI available globally via symlink |
| go | `go install ./cmd/...` or `go install .` | Binary in `$GOPATH/bin` |

### Flags

- `--global` (default): install as a global tool
- `--venv`: install in the project's venv only (pypi: `uv sync`, npm: `npm install`, go: N/A)
- `--uninstall`: reverse the install (`uv tool uninstall <name>`, `npm unlink`, etc.)

### Monorepo support

For monorepos (`.rlsbl-monorepo/`), `rlsbl dev install` in the root installs all sub-projects. Or `rlsbl dev install <workspace>` for a specific one.

## Context

Observed in a private project: after scaffolding and releasing to PyPI, the developer wanted the local source installed editably. Had to figure out `uv tool install -e .` manually.
