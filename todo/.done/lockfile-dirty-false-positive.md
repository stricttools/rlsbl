# Lockfile modifications from version bump flagged as concurrent changes

## Problem

During `rlsbl release run`, the version bump step (step 12) updates `package.json` and `pyproject.toml`, which triggers lockfile regeneration (`uv.lock` and `package-lock.json`). The release pipeline itself acknowledges these updates:

```
Updated version in package.json
Synced version to pyproject.toml
Synced version to selfdoc.json
Lockfile updated: uv.lock
Lockfile updated: package-lock.json
```

But then the concurrent-change detection rejects these same files:

```
Unexpected modified files detected (possible concurrent change): package-lock.json, uv.lock. Aborting release.
Local state has been rolled back to <sha>.
```

This makes it impossible to release projects that have both `npm` and `pypi` targets (or any project where version bumps cause lockfile changes).

## Reproduction

Reproducible on rlsbl 0.48.1 with selfdoc (which has `targets: ["npm", "pypi", "docs"]`). Running `rlsbl release run --watch --yes` fails every time at the same point.

## Expected behavior

Lockfile modifications that are a direct consequence of the version bump should be included in the release commit, not flagged as concurrent changes. The dirty-check after the version bump should account for known side effects of the bump (lockfile regeneration, selfdoc hash refresh, etc.).

## Affected files

- The concurrent-change detection logic in the release pipeline (wherever `Unexpected modified files detected` is emitted)

## Effort

Small to medium. The fix likely involves maintaining a list of expected modified files during the version bump step and excluding them from the concurrent-change check.
