# Extend rlsbl watch to accept workflow run IDs

## Problem

`rlsbl watch` currently only accepts a commit SHA and polls `gh run list --commit <sha>` for workflow runs. When `release retry` dispatches workflows via `gh workflow run`, the dispatched runs may not be associated with the release commit SHA (especially if the dispatch ref differs from the tag commit). The watch command can't monitor them.

## Proposed solution

Two changes:

**Strategy A — `--run-id` flag on watch:**
- `rlsbl watch --run-id 12345678` skips `poll_runs` entirely
- Calls `gh run view <id> --json databaseId,name,status` to get run info
- Passes directly to `_watch_single_run` / `_watch_runs`
- Accepts multiple IDs for parallel watching

**Strategy B — Capture run IDs from dispatch in retry:**
- In `release_retry.py`, capture stdout from `gh workflow run` which returns the run URL ("if available")
- Parse `/actions/runs/(\d+)` from the URL
- If no URL returned, fall back to polling `gh run list --workflow <filename> --limit 1`
- Pass collected run IDs to watch via `--run-id`

Both strategies needed together: A adds the capability, B wires retry to use it.

## Edge cases

- `gh workflow run` URL return is documented as "if available" — race condition by design. Fallback polling needed.
- Mixing SHA-based and run-ID-based watching should be disallowed (use one or the other)
- Run IDs that have already completed should still work (`gh run watch` handles this)

## Effort

Medium. New flag on watch, run-ID capture in retry, fallback polling logic.
