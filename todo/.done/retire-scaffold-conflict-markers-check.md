# Retire scaffold-conflict-markers check (quality.py)

## Context

The `scaffold-conflict-markers` check in `rlsbl/checks/quality.py` (line ~253) is now superseded by the new `scaffold-conflicts` check in `rlsbl/checks/project.py`. The old check:

- Tags: `quality` only (not enforced at push or release)
- Detection: flags any line containing bare `=======` (false-positive prone)
- Scope: scans `.rlsbl/**` and workflow files

The new check:

- Tags: `project`, `prepush`, `release` (enforced at push via pre-push hook and at release via explicit pre-mutation call in release.py)
- Detection: requires both `<<<<<<< ` AND `>>>>>>> ` in the same file (no false positives)
- Scope: managed-files.json + `.github/workflows/` + `.rlsbl/hooks/`
- Has explicit release.py integration (`_abort_on_scaffold_conflicts()`)

The old check is the reason six consumer repos shipped corrupted publish.yml files undetected — its `quality` tag meant it never ran during push or release.

## Work

1. Remove the `scaffold-conflict-markers` check function from `rlsbl/checks/quality.py`
2. Remove its entry from `rlsbl/data/checks.toml`
3. Remove it from `CHECK_TARGETS` in `rlsbl/checks/__init__.py`
4. Update `EXPECTED_CHECKS` in `tests/test_doctor_checks_migration.py`
5. Remove or update any tests specific to the old check (grep for `scaffold-conflict-markers` in tests/)
6. Verify: `uv run pytest -q` passes with the old check removed

## Scope

Small. One check function + its registration in three places + test updates.
