# release-init blind to releasable-level and workspace-root targets

## Problem

`rlsbl monorepo release-init` fails with "no eligible releasables with detected targets" in monorepos where:

1. Individual member projects have `"targets": []` (they're bundled into a releasable, not published independently)
2. The actual target (e.g., pypi) is configured at the **workspace root** level (`.rlsbl/config.json`)
3. The releasable-level config (`.rlsbl-monorepo/releasables/<name>/config.json`) has no `targets` key

The result: the user must manually write `.rlsbl-monorepo/releases/unreleased.toml` before every release, including the `[releasables.<name>]` section, bump type, description, context, and `include = ["pypi"]`. This happens every release cycle because post-release cleanup creates an empty file.

## Root cause

`_collect_releasable_targets()` in `batch_release_init.py` iterates member projects and calls `detect_targets()` on each. When every member has `"targets": []` (an explicit empty list), `detect_targets` respects the explicit config and returns `[]`. The union across all members is empty. The function never checks:

- The releasable-level config directory for a `targets` key
- The workspace root config (which has the actual targets)

The same blind spot affects the `workspace-targets` check in `workspace.py` (separate todo already filed).

## Second issue: post-release creates empty file

After a successful release, `_finalize_batch_file()` in `batch_release.py` renames the release file to a timestamped archive and creates a completely empty `unreleased.toml`. Even if `release-init` worked, the user would still need to run it manually after each release. The post-release flow could either auto-run `release-init` or scaffold a minimal template.

## Expected behavior

After a release completes, the next `unreleased.toml` should be pre-scaffolded with:

```toml
[releasables.<name>]
bump = ""
description = ""
context = ""
include = ["<detected targets>"]
exclude = []
```

This requires either:
1. `release-init` working correctly (by detecting targets from workspace root / releasable config)
2. Post-release cleanup calling `release-init` automatically
3. Post-release cleanup scaffolding the file directly using the targets from the just-completed release

## Impact

Every release cycle requires the user to manually author the release file from scratch. For a project releasing frequently (3 releases in one session), this is significant friction. The `bump`, `include`, and `exclude` fields are nearly always the same — only `description` and `context` change.
