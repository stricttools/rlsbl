# `rlsbl release` finalization doesn't generate the per-version `.md` file

## Context

After `rlsbl release minor --yes` completes successfully, `.rlsbl/changes/x.y.z.md` is missing while `.rlsbl/changes/x.y.z.jsonl` exists. The release flow's step 11 (per CLAUDE.md) is supposed to "generate `x.y.z.md` alongside the JSONL," but it doesn't.

## Observation

After v0.35.0 release:
- `.rlsbl/changes/0.35.0.jsonl` — present, chmod 444 ✓
- `.rlsbl/changes/0.35.0.md` — **missing**
- `.rlsbl/changes/0.34.0.md` — present (prior release)
- `.rlsbl/changes/0.33.0.md` — present (prior release)

Workaround: run `rlsbl changelog generate` after the release. This produces the missing `.md` (and the command auto-commits the result).

## Root cause hypothesis

This was likely introduced by Phase 3 of session 4 (the `generate_changelog` consolidation that removed the post-finalize regeneration call in `commands/release.py`). The deleted third call site was responsible not just for replacing `## Unreleased` with the version heading, but ALSO for generating the per-version `.md` file as a side effect (`generate_changelog` calls `generate_version_file` for each versioned entry).

After Phase 3's consolidation:
- The second `generate_changelog` call (with `version_override=new_version`) handles CHANGELOG.md
- `finalize_version` renames `unreleased.jsonl` → `x.y.z.jsonl`
- But nothing calls `generate_version_file(changes_dir, new_version)` to produce `x.y.z.md`

## Fix direction

Add an explicit `generate_version_file(changes_dir, new_version)` call right after `finalize_version` in `commands/release.py:_run_release_mutating`. Or, simpler: call `generate_changelog(version_dir)` (without `version_override`) after `finalize_version` — it'll iterate `list_versioned_files` (which now includes the new version) and produce both the per-version `.md` and refresh `CHANGELOG.md`.

The cleaner fix is the explicit single-file call.

## Affected files

- `rlsbl/commands/release.py` `_run_release_mutating` (around the `finalize_version` call)
- Add a test in `tests/test_release_*.py` that asserts the per-version `.md` file exists after a successful release

## Effort

Small. Single function call + a regression test.

## Related work

- The Phase 3 consolidation that introduced this regression is at commit `c00a8d5` (per the v0.35.0 changelog).
- The other rlsbl release-flow bug discovered in Phase 4 recovery ("unexpected modified files check trips on changelog-generated files") was NOT hit during v0.35.0 — only this one. May still be worth filing if it recurs.
