# Scaffold doesn't respect npm subdirectory paths

## Problem

When an npm target is configured with a non-root path (e.g., `"path": "npm/"` in config.json), multiple scaffold functions hardcode `"."` instead of using the target's path:

1. `_finalize_scaffold()` calls `ensure_tags(registries)` without paths. `ensure_npm_keyword(".")` tries to open `./package.json` at root → FileNotFoundError.
2. `_check_npm_lockfile_missing()` walks up from `"."`, never into subdirectories.
3. `NpmTarget.template_mappings()` calls `_detect_package_manager(".")` with hardcoded root.
4. `check_project_exists(".")` checks root, gives misleading "no npm project found" error.

The scaffold still writes all files despite the error (exit code 1 but non-fatal), so the crash is cosmetic but confusing.

## Affected projects

pgdesign, migrable, saferm — any project with npm target in a subdirectory.

## Fix

Thread target paths through the scaffold pipeline. `_finalize_scaffold` needs per-target paths. `ensure_tags` needs to accept paths per registry. `_detect_package_manager` and `_check_npm_lockfile_missing` need to receive the target path.

## Effort

Medium. Multiple functions need path parameters added and threaded through callers.
