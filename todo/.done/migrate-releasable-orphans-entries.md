# migrate-releasable orphans per-project changelog entries

## Problem

When `rlsbl monorepo migrate-releasable` migrates a package into a releasable group, any entries in the package's per-project `unreleased.jsonl` are silently orphaned. They remain in the now-dead per-project changelog file and are never carried into the releasable-level changelog.

This caused approximately 22 user-facing entries to be lost from a monorepo's released history. The entries were written to per-project files before migration, and after migration those files are no longer consulted by the release flow.

## Proposed fix

The `migrate-releasable` command should either:

1. **Merge**: automatically move per-project unreleased entries into the releasable-level `unreleased.jsonl` during migration.
2. **Hard error**: refuse to migrate if the per-project `unreleased.jsonl` is non-empty, forcing the user to handle them first (merge manually, release the package, or explicitly discard).

Option 2 is safer and more aligned with the "hard errors, not warnings" philosophy -- it makes the user deal with the entries before the migration proceeds, preventing silent data loss.

## Affected files

- `rlsbl monorepo migrate-releasable` command implementation
- Possibly the validation/check logic that runs pre-migration
