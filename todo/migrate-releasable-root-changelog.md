# migrate-releasable ignores root changelog

## Problem

`rlsbl monorepo migrate-releasable` consolidates per-package `.rlsbl/changes/unreleased.jsonl` into releasable-level changelogs, but it ignores the root `.rlsbl/changes/unreleased.jsonl`.

In monorepos where changelog entries were tracked at the root level (before releasables existed), the root file contains the actual feature changelog — including user-facing entries with descriptions and types. After migration, these entries are orphaned: they're not in any releasable's changelog and can never be finalized through a release.

## Impact

The first releasable release has no user-facing entries in its changelog (they're all in root). Users must manually move entries from root to releasable, partitioning by commit range (pre-tag vs post-tag) and attributing commits to releasables via file path analysis. This is error-prone.

## Proposed solution

`migrate-releasable` should include an option to consolidate root-level changelog entries into the target releasable. Entries would be partitioned by commit range (pre-migration-tag goes to a versioned .jsonl backfill, post-tag goes to unreleased.jsonl). Commits are attributed to releasables by checking which member directories they touch. Cross-cutting commits (touching no member directory) go to the primary releasable.

## Affected files

- `rlsbl/releasable_migration.py` — `consolidate_changelogs()` function
