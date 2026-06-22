# Auto-commit manifest during release

## Problem

When `selfdoc gen` runs during `rlsbl release run`, it regenerates `.selfdoc/manifest.json`. This leaves the working tree dirty, causing `rlsbl release run` to fail with "working tree is not clean." The workaround is to manually commit the manifest before retrying the release — a frustrating loop that happens on nearly every release.

## Context

- `rlsbl release run` calls `selfdoc gen --no-commit` then `selfdoc check`
- `selfdoc gen` regenerates `manifest.json` if any doc content changed (descriptions, hashes, etc.)
- The `--no-commit` flag is used because rlsbl manages commits during release
- But rlsbl only commits files it knows about — it doesn't know that selfdoc modified `manifest.json`

## Proposed fix

`selfdoc gen` should auto-commit `manifest.json` when it changes, even with `--no-commit`. The `--no-commit` flag should apply to generated doc files, not to the internal manifest. Alternatively, rlsbl's selfdoc integration should include `.selfdoc/manifest.json` in its managed file list.

## Affected projects

Every project using both rlsbl and selfdoc hits this on every release where doc content changes.
