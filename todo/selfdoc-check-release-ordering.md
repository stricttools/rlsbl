# Move selfdoc check earlier in the release pipeline

## Problem

When rlsbl releases a project that uses selfdoc, `selfdoc check` runs in the built-in lint phase (step 4 of release, after pre-checks and tests). If STALE001 fires (page description no longer matches content), the release aborts mid-way. The user fixes the description, adds a changelog entry, and retries — 2-3 reactive commits per release.

## Proposed fix

Move `_run_selfdoc_check()` from the lint phase (release.py, currently after `_run_builtin_lint`) to before `_run_builtin_tests`. This catches staleness errors before any mutation, at a cost of ~300ms per release.

## Why not a pre-checks hook?

Pre-checks hooks are user-configured bash scripts. Zero of 12 selfdoc consumer projects have configured one despite the hook existing for months. If validation is important enough to block releases, it should be part of the official pipeline, not an opt-in script.

## Effort

Small. Move one function call in release.py.
