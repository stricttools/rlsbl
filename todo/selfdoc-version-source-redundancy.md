# selfdoc's version_source is redundant with rlsbl's version bump

## Problem

selfdoc v0.16.0 added a `version_source` config field that reads the project version from a manifest file (pyproject.toml, package.json, etc.) to avoid version drift in selfdoc.json. But rlsbl already bumps selfdoc.json's `version` field during releases via `_bump_selfdoc_version`. For rlsbl-managed projects, version drift is already prevented — `version_source` solves a problem that doesn't exist.

The only scenario where drift occurs is when selfdoc.json is created with an initial version that's never been released through rlsbl (e.g., mobileinfra had `"version": "0.0.0"` but pyproject.toml had `0.1.0`). This is a one-time setup issue, not an ongoing problem.

## Impact

17 projects were migrated to add `version_source` to their selfdoc.json. This added a config field that duplicates what rlsbl already does, plus added ~100 lines of config loading code in selfdoc (manifest reading, conflict detection, VER check updates).

## Questions

- Should rlsbl's `_bump_selfdoc_version` be the canonical solution, making `version_source` unnecessary?
- Should selfdoc remove `version_source` and rely on rlsbl? Or keep it for non-rlsbl projects?
- Is there a way to prevent the initial-version-mismatch problem (e.g., rlsbl scaffold sets selfdoc.json version from the manifest during scaffolding)?
