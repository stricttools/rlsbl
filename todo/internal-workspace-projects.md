# Internal workspace projects (no release, no user-facing requirement)

## Problem

Monorepo workspace projects that are purely internal test infrastructure (e.g., conformance test suites) are forced through the same release flow as user-facing packages. This creates overhead:

- User-facing changelog entry required, but all changes are internal
- GitHub Releases created with no downloadable artifacts and no consumers
- Version numbers that nobody references or depends on
- Release coordination that adds no value

The `library` flag exists but only controls lint behavior — it doesn't affect release or changelog requirements.

## Proposed solution

Add an `internal = true` flag to workspace project entries in `workspace.toml`:

```toml
[[projects]]
path = "conformance/"
name = "conformance"
internal = true
watch = ["python/**", "go/**"]
```

When `internal = true`:

- `changelog-user-facing` check is skipped (all entries are expected to be non-user-facing)
- `rlsbl release run` skips GitHub Release creation
- Version bumps still happen (for internal consistency tracking)
- Changelog coverage and pre-push checks still apply (commits must be tracked)
- The project still appears in `rlsbl monorepo status` but is marked as internal

## Affected code

- `rlsbl/checks.py`: `check_changelog_user_facing` should skip for internal projects
- `rlsbl/commands/release.py`: skip GitHub Release creation for internal projects
- `rlsbl/commands/monorepo/commands.py`: parse and display `internal` flag
- `rlsbl/snapshot.py`: include `internal` in snapshot

## Effort

Small-medium. The flag plumbing is straightforward. The main work is deciding which release steps to skip vs keep.
