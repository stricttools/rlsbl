# Resume hardening: four defects from live recoveries

## Context

Two real mid-release recoveries (a monorepo releasable release and a standalone consumer
release, 2026-08-08) exposed four resume-path defects. All reproduced live.

## Problems

1. Phase-A BUILD steps re-run on resume after VERSION_BUMPED completed, and a re-run build
   failure is misattributed to CI_VERIFIED in failed_steps.
2. From a member cwd, `release resume` exits 1 SILENTLY — a bare
   `except ReleaseAbortError: sys.exit(1)` (release/__init__.py ~215) discards the message.
3. `release resume --dry-run` succeeds from cwds where the real resume dies — the preview
   cannot catch the exact condition that kills the real run.
4. An ABORTED resume corrupts its own state file: the foreign-commit guard overwrites
   pre_release_sha with the observed HEAD before aborting, so any failed post-push step
   whose remedy is a config-change commit becomes unresumable (catch-22; repaired by hand
   once, live).

## Solutions

(1) marker builds like other steps; (2) print the abort message before exiting; (3) route
dry-run through the real resume preflight; (4) never persist a re-pin on the abort path.
Red-green each.

## Effort

Medium; all inside the release engine's resume path.
