# Releasable migration edge cases

The releasable migration tooling (`consolidate_changelogs()`, `cleanup_per_package_release_state()`, `create_migration_tag()`) has edge cases that surface during real migrations. These are in addition to the missing CLI command (covered in `releasable-model-gaps.md`).

## 1. Coverage scope expansion on consolidation

Per-package changelogs cover commits within each package's path. The releasable-level changelog covers ALL commits in the repo (or at least all commits since the last releasable tag). When consolidating, commits that touched only cross-cutting paths (CI, docs, scripts, root config, historical renames) have no per-package entry. After consolidation, these commits are in-scope but uncovered, causing `rlsbl check --tag changelog` to fail.

The consolidation should either:
- Auto-create `--no-user-facing` entries for newly-in-scope commits
- Or create a migration tag at the consolidation point so only post-migration commits need coverage

Without this, users must manually identify and create entries for every uncovered commit — potentially hundreds in a mature repo.

## 2. Batch limit exclusions lost during consolidation

Per-package `.rlsbl/config.json` files have `batch_limits.exclusions` for large entries (e.g., initial implementation entries referencing dozens of commits). `consolidate_changelogs()` merges the changelog entries but does not carry over the exclusions to the releasable-level config. The consolidated changelog then fails `batch-size-commits` validation because the exclusions no longer exist.

The consolidation should merge exclusions from all member packages into the releasable-level configuration.

## 3. Cross-package commit dedup may violate max_entries_per_commit

A single commit can appear in multiple per-package changelogs (e.g., a refactoring commit touching 8 packages has an entry in each). After consolidation into one file, that commit appears in 8 entries, potentially exceeding `max_entries_per_commit` (default 5).

The consolidation should either:
- Merge entries sharing the same commit set into a single entry with a `packages` list
- Or auto-create exclusions for commits that exceed the limit due to consolidation

## 4. Scaffold recreates per-package changelog infrastructure

After `cleanup_per_package_release_state()` removes per-package `.rlsbl/changes/` directories, running `rlsbl scaffold` recreates them because scaffold does not check whether a project belongs to a releasable. This forces the cleanup to be re-run after every scaffold, or requires manual ordering discipline.

Scaffold should skip creating per-package changelog infrastructure for projects that are members of a releasable.

## Affected files

- `releasable_migration.py` — `consolidate_changelogs()` needs scope expansion handling, exclusion merging, and commit dedup
- `releasable_cleanup.py` — `cleanup_per_package_release_state()` results are undone by scaffold
- `commands/scaffold.py` (or equivalent) — needs releasable membership awareness for changelog directory creation
