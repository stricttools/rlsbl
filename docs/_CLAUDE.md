---
description: "Operational reference for AI agents working on rlsbl-managed projects."
---
# rlsbl

:-: var key="project.description"

Built in Python 3.11+ with ruamel-yaml, tomlkit, strictcli, and tree-sitter. Also distributed as an npm wrapper package. Current version: check `package.json`.

## Commands

:-: table-commands

## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Run `rlsbl release init` to scaffold `.rlsbl/releases/unreleased.toml`
- Edit the release file: set bump type (patch/minor/major), include/exclude targets
- Run `rlsbl release run --no-allow-dirty --watch --yes` to execute the release
- CI handles publishing automatically via the publish workflow
- Never publish manually -- always use `rlsbl release run`
- Use `rlsbl release run --dry-run` to preview without making changes
- Global flags `--dry-run`, `--yes`, `--quiet` are available on all commands
- Release-specific required flags: `--allow-dirty`/`--no-allow-dirty`, `--watch`/`--no-watch` (no defaults -- must choose explicitly)

## Release pipeline order

During `rlsbl release run`, the validation and build steps run in this order:

1. Pre-checks hook (`.rlsbl/hooks/pre-checks.sh`)
2. Strictcli schema dump (`--dump-schema`, for projects using strictcli)
3. Selfdoc gen (`selfdoc gen --no-auto-commit`, regenerates docs from source)
4. Selfdoc check (verifies generated docs are up-to-date)
5. Built-in tests (`uv run pytest`, `go test`, `npm test`) -- skipped if pre-release hook is customized
6. Built-in lint (library projects only) -- skipped if pre-release hook is customized
7. Pre-release hook (`.rlsbl/hooks/pre-release.sh`)

**Hooks override:** When the pre-release hook has been customized (content differs from any known scaffold template version), built-in tests and lint are skipped. The hook is expected to handle testing and linting itself.

## Conventions

- No tokens or secrets in command-line arguments (use env vars or config files)
- All file writes to shared state should be atomic (write to tmp, then rename)
- External calls (APIs, CLI tools) must have timeouts and graceful fallbacks
- Use `rlsbl dev install` for local editable installs (picks the right tool per target)
- CI runs smoke tests on every push; manual testing for UI/UX changes

## Configuration

`.rlsbl/config.json` holds per-project settings. Key fields:

- `pipelines` -- publish pipelines with mandatory `type` and `local` fields. See [docs/pipelines.md](pipelines.md).
- `private` (bool, required) -- blocks publishing to public registries when `true`. No default.
- `release_branches` -- branch names that trigger manual-release-push warnings.
- `batch_limits` -- limits for changelog batch size checks. See [docs/changelog.md](changelog.md).

For details on checks, CI customization, pre-push hook, validation cache, monorepo support, and asset uploads, see the corresponding pages in [docs/](index.md).
