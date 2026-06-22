# Releasable model gaps (remaining)

## No CLI command for releasable migration

The migration from per-package to per-releasable mode requires calling Python functions directly (`consolidate_changelogs()`, `consolidate_versions()`, `create_migration_tag()`, `cleanup_per_package_release_state()`). There is no CLI command like `rlsbl monorepo migrate-releasable`.

A CLI command should: read workspace.toml, consolidate per-package changelogs into the releasable-level changelog (with `packages` attribution), clean up per-package changelog directories (via saferm), create migration tags, and report what it did.

Note: `create_migration_tag()` has a bug where the fallback `v*` glob matches unrelated tags when historical per-package tags used `/` separator. Needs to be fixed or the fallback removed.

## Affected files

- `releasable_migration.py` — functions exist but no CLI entry point
- `releasable_cleanup.py` — `cleanup_per_package_release_state()` exists but no CLI entry point
