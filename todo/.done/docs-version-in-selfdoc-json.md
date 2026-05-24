# Add version field to selfdoc.json and wire DocsTarget

## Problem

DocsTarget returns hardcoded "0.0.0" and has no version file. The skip workaround (v0.41.0) hides it from version-consistency checks but doesn't solve the underlying issue: docs should be a properly versioned target.

## Implementation

### selfdoc side
- Add `"version"` field to selfdoc.json schema
- `selfdoc gen` / `selfdoc build` should read it (for versioned output metadata)
- `selfdoc deploy` already reads version from pyproject.toml — it should prefer selfdoc.json's own version when present

### rlsbl side
- `DocsTarget.read_version()`: read `selfdoc.json["version"]`
- `DocsTarget.write_version()`: write `selfdoc.json["version"]` (atomic, via tomlkit or json round-trip)
- `DocsTarget.version_file()`: return `"selfdoc.json"`
- Remove the skip workaround in the version-consistency check (targets with `version_file() is None`)

### Consumer backfill
- Add `"version": "<current_project_version>"` to every consumer project's selfdoc.json

## Replaces

- `todo/.defer/docs-target-versioning.md`
- `todo/.defer/selfdoc-version-field.md`

## Effort

Small. Both sides are straightforward JSON field read/write.
