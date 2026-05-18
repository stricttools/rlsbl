# Unify config format: workspace.toml and config.json

## Problem

rlsbl uses two different configuration formats depending on the project type:

- **Standalone projects**: `.rlsbl/config.json` (JSON)
- **Monorepo root**: `.rlsbl-monorepo/workspace.toml` (TOML)

Having different formats for different concerns is confusing. Users and AI agents working across both project types must know which format to use where, and the codebase has separate parsing paths for each.

## Proposed solution

Unify to a single format -- TOML is the better candidate:

- More human-friendly (comments, multiline strings, less punctuation noise)
- Already a dependency (tomlkit is rlsbl's sole runtime dependency)
- Aligns with the Python ecosystem (pyproject.toml, cargo.toml, etc.)

### Migration path

1. Add TOML support to standalone config loading (read `config.toml` alongside `config.json`).
2. If both exist, error with a clear message telling the user to migrate.
3. Provide a `rlsbl config migrate` command that converts `config.json` to `config.toml` and removes the old file.
4. Keep JSON reading as a fallback for one major version cycle, then remove it.
5. Update `rlsbl scaffold` to generate `config.toml` instead of `config.json`.

### Config key unification

Beyond the format, consider whether standalone and monorepo configs should share a common schema where possible. For example, keys like `changelog_format`, `target`, and hook paths could use the same names and structure in both contexts.

## Affected files

- `rlsbl/commands/config_cmd.py` -- config display and migration
- `rlsbl/config.py` or equivalent config loading module
- `rlsbl/commands/scaffold_cmd.py` -- template generation
- `rlsbl/commands/release_cmd.py` -- config consumption
- Monorepo workspace loading code

## Effort estimate

Medium. The format conversion is mechanical, but the migration command, fallback logic, deprecation warnings, and ensuring all config consumers handle both formats during the transition period add up.
