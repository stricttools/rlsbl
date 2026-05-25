# pgdesign target: advanced features

## Current state

Basic pgdesign target implemented: detect, read/write version, validate on build. 22 tests passing.

## What's missing

### Migration generation on release

When the schema changes between releases, the target should:
1. Run `pgdesign diff <schema_dir> --db <url>` to compare desired vs live
2. If differences exist, run `pgdesign migrate generate` to produce a migration file
3. Commit the migration file as part of the release
4. After push, apply via `pgdesign migrate apply` in the post-release hook

This requires:
- Database URL configuration in pgdesign.toml or rlsbl config
- pgdesign binary available on PATH or configurable location
- Migration file naming convention aligned with rlsbl version tags

### Pre-release schema validation

Beyond `pgdesign validate`, the release should also:
- Run `pgdesign audit` (normal form checking) and fail on errors
- Run `pgdesign diff` and show the migration preview for confirmation
- Verify no destructive changes without `--allow-dangerous` flag

### Schema version synchronization

The pgdesign.toml `[project] version` should auto-sync with the project version on release. Currently write_version() handles this, but it needs to work correctly in monorepo scenarios where the schema is a sub-project.

### CI template

Scaffold a GitHub Actions workflow that:
- Runs `pgdesign validate` on every PR
- Runs `pgdesign diff` against a test database
- Comments the migration preview on the PR
