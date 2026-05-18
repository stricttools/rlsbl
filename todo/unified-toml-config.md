# Unified TOML config format

## Context

rlsbl currently uses two config formats:
- `.rlsbl/config.json` — per-project release settings (JSON)
- `.rlsbl-monorepo/workspace.toml` — monorepo project registry (TOML)

This split is confusing — users and AI agents must know which format to use where, and the codebase has separate parsing paths.

## Decision points

Two sub-decisions to settle during implementation:

1. **Migration target format**: TOML is the agreed format (already a dependency via tomlkit; supports comments; aligns with Python ecosystem).

2. **File layout**: Two options to choose between:
   - (a) Keep `config.toml` and `workspace.toml` as separate files. Same format, clear separation of concerns. Lower migration risk.
   - (b) Merge into a single `config.toml` per project. Monorepos add a `[[workspace.projects]]` section. Everything in one place but standalone projects have unused workspace section.

User's stated preference: "i like options 1 and 2 but this will have a big blast radius."

## Implementation outline

1. Add TOML reading to `rlsbl/config.py` `read_project_config()`. Try `config.toml` first, fall back to `config.json` with deprecation warning.
2. Error if both `config.json` and `config.toml` exist — user must choose.
3. Re-create the `rlsbl config` command (deleted in v0.15.0). Add `rlsbl config migrate` to convert config.json → config.toml.
4. Update `rlsbl scaffold` to generate `config.toml` for new projects.
5. Fix `save_workspace()` in `rlsbl/workspace.py` to round-trip with tomlkit (preserve comments) instead of rebuilding from scratch.
6. Keep JSON fallback for one major version cycle, then remove.
7. Update all 17+ call sites that read config (per earlier audit).

## Affected files

- `rlsbl/config.py` — main loader
- `rlsbl/workspace.py` — workspace.toml save round-trip fix
- `rlsbl/commands/config_cmd.py` (new) — config show/migrate
- `rlsbl/__init__.py` — re-register config command
- 17+ call sites in commands/, targets/, changelog/, npm_wrapper.py

## Effort

Medium-large. The format conversion is mechanical, but the migration command, fallback logic, deprecation warnings, ensuring all consumers handle both formats during the transition, and the round-trip fix in tomlkit add up.

## Related work

- `todo/.obsolete/unify-config-format.md` — earlier framing of this same problem
- `todo/target-rename-split.md` — the rename effort would also touch consumer config files; coordinate migrations
