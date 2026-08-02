# Migrate fleet to changeset-file coverage mode

## Context

Phase 8 added `coverage_unit = "changeset-file"` as an alternative to the
existing SHA-based `coverage_unit = "commit"` mode. The new mode uses
individual pending JSON files in `.rlsbl/changes/pending/` instead of
commit-hash-based entries in `unreleased.jsonl`.

## What needs to happen

1. Evaluate which projects benefit from changeset-file mode (projects with
   frequent rebases, squash-merge workflows, or multi-session concurrent
   development are the strongest candidates).

2. For each project being migrated:
   - Set `"coverage_unit": "changeset-file"` in `.rlsbl/config.json`
   - Create the `.rlsbl/changes/pending/` directory
   - Existing `unreleased.jsonl` entries should be manually converted to
     pending files if needed, or released first to clear the unreleased
     state.

3. Update any CI scripts or hooks that assume commit-mode behavior.

4. The fleet sweep script `scripts/sweep_coverage_unit.py` sets all
   configs to `"commit"` mode. A separate migration script would be
   needed to flip specific projects to `"changeset-file"`.

## Why deferred

The infrastructure is in place (Phase 8 complete), but the migration is
a per-project decision that requires evaluating each project's workflow.
No urgency -- `commit` mode continues to work exactly as before.
