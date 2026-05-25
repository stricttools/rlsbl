# Run `selfdoc gen` before `selfdoc check` during release

## Problem

`rlsbl release` runs `selfdoc check` (line 755 of `release.py`) to validate directives, coverage, and SEO -- but never runs `selfdoc gen`. When source code changes between releases (new modules, changed docstrings, new CLI commands), the generated doc pages in `docs/` become stale. `selfdoc check` then validates stale pages, which can either pass silently on outdated content or fail on undocumented new symbols.

Real-world case: `todo/.done/selfdoc-check-failures-v0.32.md` documents a release where undocumented new modules caused check failures. Running gen first would have prevented it.

In claudewheel, 37 stale/missing doc files accumulated because `selfdoc gen` hadn't been re-run since new features were added to selfdoc (strictcli CLI page generation, improved description extraction).

## Proposed fix

Add a `_run_selfdoc_gen()` step in `release.py` that runs `selfdoc gen --no-commit` immediately before `_run_selfdoc_check()` (before line 755). The `--no-commit` flag is important because rlsbl manages its own commits.

The `hook_generated` file tracking mechanism (lines 732-784) already handles files created during the pre-release phase: it snapshots dirty files before and after, and includes the diff in the release commit. `selfdoc gen` should run within this snapshot window so any generated/updated files are automatically included in the release commit.

The new function should mirror `_run_selfdoc_check()` structure:
- Skip if `selfdoc.json` doesn't exist
- Skip if `selfdoc` CLI is not installed
- Skip if `docs` is in the release file's `exclude` list
- Skip on `--dry-run`
- Fatal on non-zero exit

## Affected files

- `rlsbl/commands/release.py` -- add `_run_selfdoc_gen()`, call it before `_run_selfdoc_check()`

## Effort

Small. ~20 lines mirroring the existing `_run_selfdoc_check()` pattern.
